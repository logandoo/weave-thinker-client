"""TDD tests — voice main-LLM stream stall guard (2026-09-20).

用户报告 60 生产 conv 213f5a55：最后一个 query 后对话彻底卡死、5 分钟无回复、
后续 query 被吞。根因族之一：voice 主生成 `async for ev in
svc.stream_chat_structured(...)` 没有消费端停滞守卫 —— 上游挂起时
`_heartbeat_wrapped` 只发心跳哨兵，循环永远不结束，`_handle_user_turn` 卡在
`await gen_task`，responder 阻塞，后续所有轮次被吞。

守卫（`voice_llm_ttft_timeout_seconds` / `voice_llm_inactivity_timeout_seconds`）：
- 首字未到且流已打开超过 TTFT 上限 → 终止并 error 事件；
- 首字已到但块间超过 inactivity 上限 → 终止并 error 事件；
- 正常流动不误杀。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_voice_generation_guard.py -v
"""
import asyncio

from app.services.voice_service import VoiceDuplexSession


class _DummyWS:
    def __init__(self):
        self.sent = []
        self.json_events = []

    async def send_bytes(self, data):
        self.sent.append(data)

    async def send_text(self, text):
        self.json_events.append(text)


class _FakeClient:
    base_url = "https://api.deepseek.com/v1"


class _FakeLLM:
    """Streaming fake: yields given events, sleeping between them."""

    def __init__(self, events, gap=0.05):
        self._events = events
        self._gap = gap
        self.client = _FakeClient()

    async def stream_chat_structured(self, messages, **kwargs):
        for ev in self._events:
            await asyncio.sleep(self._gap)
            yield ev


def _make_session():
    ws = _DummyWS()
    s = VoiceDuplexSession(ws, None, None, conversation_id=None)

    async def _send_json(ev):
        ws.json_events.append(ev)

    s._send_json = _send_json
    return s, ws


def _errors(ws):
    return [e for e in ws.json_events if e.get("event") == "error"]


async def _consume_flush(s):
    while True:
        item = await s._tts_queue.get()
        if isinstance(item, dict) and "flush" in item:
            fut = item["flush"]
            if fut and not fut.done():
                fut.set_result(True)
        if item is None:
            return


def test_ttft_timeout_aborts_stalled_stream(monkeypatch):
    """No first content token within the TTFT bound → error, loop exits."""

    async def run():
        s, ws = _make_session()
        monkeypatch.setitem(s.config.voice, "llm_ttft_timeout_seconds", 0.2)
        monkeypatch.setitem(s.config.voice, "llm_inactivity_timeout_seconds", 10.0)
        fake = _FakeLLM([{"type": "heartbeat", "data": None}] * 10, gap=0.05)
        s._build_llm = lambda purpose="voice": (fake, "fake-model")

        await asyncio.wait_for(
            s._generate_and_speak("测试一下", allow_filler=False), timeout=10
        )

        errs = _errors(ws)
        assert errs, f"expected a timeout error event, got {ws.json_events}"
        msg = errs[0].get("error", "")
        assert "超时" in msg or "timeout" in msg.lower()

    asyncio.run(run())


def test_inactivity_timeout_after_first_token(monkeypatch):
    """A stall AFTER the first content token is bounded too.

    Post-content stalls deliver the partial answer and end the turn (no
    duplicate error event by design) — the discriminator is that the turn
    RETURNS despite an endless stalled stream (pre-guard it would hang)."""

    async def run():
        s, ws = _make_session()
        monkeypatch.setitem(s.config.voice, "llm_ttft_timeout_seconds", 5.0)
        monkeypatch.setitem(s.config.voice, "llm_inactivity_timeout_seconds", 0.2)

        class _EndlessStallLLM:
            client = _FakeClient()

            async def stream_chat_structured(self, messages, **kwargs):
                yield {"type": "content", "data": "你好"}
                while True:
                    await asyncio.sleep(0.02)
                    yield {"type": "heartbeat", "data": None}

        s._build_llm = lambda purpose="voice": (_EndlessStallLLM(), "fake-model")

        consumer = asyncio.create_task(_consume_flush(s))
        t0 = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(
                s._generate_and_speak("测试一下", allow_filler=False), timeout=5
            )
        finally:
            consumer.cancel()
        elapsed = asyncio.get_running_loop().time() - t0
        assert elapsed < 2.0, f"stalled stream was not aborted (took {elapsed:.1f}s)"
        done = [
            e
            for e in ws.json_events
            if e.get("event") == "assistant_text" and e.get("done")
        ]
        assert done, f"partial answer must still be finalized: {ws.json_events}"

    asyncio.run(run())


def test_healthy_stream_no_timeout(monkeypatch):
    """A stream flowing well below the bounds must NOT be aborted."""

    async def run():
        s, ws = _make_session()
        monkeypatch.setitem(s.config.voice, "llm_ttft_timeout_seconds", 0.5)
        monkeypatch.setitem(s.config.voice, "llm_inactivity_timeout_seconds", 0.5)
        events = [
            {"type": "content", "data": "你好。"},
            {"type": "content", "data": "我在听。"},
        ]
        fake = _FakeLLM(events, gap=0.05)
        s._build_llm = lambda purpose="voice": (fake, "fake-model")

        consumer = asyncio.create_task(_consume_flush(s))
        try:
            await asyncio.wait_for(
                s._generate_and_speak("测试一下", allow_filler=False), timeout=10
            )
        finally:
            consumer.cancel()

        assert not _errors(ws), f"healthy stream must not error: {ws.json_events}"
        done = [
            e
            for e in ws.json_events
            if e.get("event") == "assistant_text" and e.get("done")
        ]
        assert done, f"expected a done assistant_text, got {ws.json_events}"

    asyncio.run(run())
