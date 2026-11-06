"""TDD tests — barge-in classifier fail-safe = defer (2026-09-20).

用户报告 query 被吞（卡死轮期间）。`_classify_barge_in` 在子代理无可用输出
（超时/坏 JSON）时旧行为回退 backchannel —— 播放中可能真实的新 query 被静默
丢弃（只播报 backchannel，不入队）。确定性噪声/回声预过滤已在分类器之前运行，
基础设施失败不应吞掉可能真实的文本：回退 defer 入队（播报结束后回答），
并用 deferred 事件告知用户，文本永不丢失。2026-11-05 起分类器故障对短应和也
统一 defer；不得再用字符表猜测语义（用户明确禁止，见 tests/decisions.md）。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_voice_barge_in_failsafe.py -v
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


class _NoChunkLLM:
    """Prefetch fake — yields nothing."""

    client = _FakeClient()

    async def stream_chat_structured(self, messages, **kwargs):
        if False:  # pragma: no cover — keep this an async generator
            yield None


def _make_session():
    ws = _DummyWS()
    s = VoiceDuplexSession(ws, None, None, conversation_id=None)

    async def _send_json(ev):
        ws.json_events.append(ev)

    s._send_json = _send_json
    s._build_llm = lambda purpose="voice": (_NoChunkLLM(), "fake-model")
    return s, ws


def test_classifier_timeout_falls_back_to_defer(monkeypatch):
    """No usable verdict + not a short ack → defer (never drop the text)."""

    async def run():
        s, ws = _make_session()

        async def _empty_classify(system, user, purpose="voice", response_format=None):
            return ""

        s._quick_classify = _empty_classify
        verdict = await s._classify_barge_in("帮我查一下明天上海的天气怎么样")
        assert verdict == "defer", f"expected defer, got {verdict!r}"

    asyncio.run(run())


def test_classifier_timeout_short_ack_also_defers_without_regex(monkeypatch):
    """No semantic character fallback: classifier outage preserves the turn as defer."""

    async def run():
        s, ws = _make_session()

        async def _empty_classify(system, user, purpose="voice", response_format=None):
            return ""

        s._quick_classify = _empty_classify
        for ack in ("嗯", "好的", "对对对"):
            verdict = await s._classify_barge_in(ack)
            assert verdict == "defer", f"{ack!r} → expected fail-safe defer, got {verdict!r}"

    asyncio.run(run())


def test_defer_verdict_queues_turn_and_emits_deferred(monkeypatch):
    """The defer verdict must enqueue the turn (text never dropped)."""

    async def run():
        s, ws = _make_session()
        monkeypatch.setitem(s.config.voice, "barge_in_enabled", True)
        monkeypatch.setitem(s.config.voice, "barge_in_proximity_gate", False)
        # Simulate active playback (barge-in path) without real TTS.
        s._speaking = True
        s._speaking_started_at = 0.0
        s._last_barge_in_time = 0.0
        s._spoken_text_recent = ""
        s._prox_seen = True
        s._prox_near = True

        async def _classify(text, history=None):
            return "defer"

        s._classify_barge_in = _classify
        await s._on_user_turn("帮我查一下明天上海的天气怎么样")

        assert not s._turn_queue.empty(), "deferred turn must be enqueued"
        events = [e.get("event") for e in ws.json_events]
        assert "deferred" in events, f"expected deferred event, got {events}"

    asyncio.run(run())
