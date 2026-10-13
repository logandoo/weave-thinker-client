"""TDD tests — first-query TTFT: identity loading must not block ASR/startup
(2026-09-20).

用户报告「第一个 query 的 TTFT 非常非常非常长」。run() 旧顺序在 `ready` 后
**串行 await `_load_identity()` + `_load_history()`，然后才启动 ASR** ——
`_load_identity` 注释自认可阻塞数十秒（共享记忆检索的 LLM 调用慢/不可达），
首句音频在 `_audio_q` 里干等，首问 TTFT = 会话启动 + 识别 + 生成。
修复：identity 加载改为后台任务与 ASR 启动并发；生成前对 identity 的等待设
硬上限（`_IDENTITY_WAIT_SECONDS`），超时用 fallback 提示词照常回答，加载完成
后下轮生效。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_voice_startup_identity.py -v
"""
import asyncio
import time
import types

import pytest

from app.services import voice_service
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
    client = _FakeClient()

    async def stream_chat_structured(self, messages, **kwargs):
        yield {"type": "content", "data": "好的。"}


def _make_session():
    ws = _DummyWS()
    s = VoiceDuplexSession(ws, None, None, conversation_id=None)

    async def _send_json(ev):
        ws.json_events.append(ev)

    s._send_json = _send_json
    s._build_llm = lambda purpose="voice": (_FakeLLM(), "fake-model")
    return s, ws


async def _consume_flush(s):
    while True:
        item = await s._tts_queue.get()
        if isinstance(item, dict) and "flush" in item:
            fut = item["flush"]
            if fut and not fut.done():
                fut.set_result(True)
        if item is None:
            return


def test_handle_user_turn_waits_bounded_for_identity(monkeypatch):
    """A still-loading identity blocks the first generation at most the cap."""

    async def run():
        s, ws = _make_session()
        monkeypatch.setattr(voice_service, "_IDENTITY_WAIT_SECONDS", 0.3)
        monkeypatch.setattr(s, "_start_memory_recall", lambda *a, **k: None)

        async def _intent(text, history=None):
            return {"should_respond": True, "needs_tools": False, "reason": ""}

        s._classify_intent = _intent

        async def _slow_identity():
            await asyncio.sleep(60)

        identity = asyncio.create_task(_slow_identity())
        s._identity_task = identity

        consumer = asyncio.create_task(_consume_flush(s))
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(s._handle_user_turn("你好啊"), timeout=15)
        finally:
            consumer.cancel()
            identity.cancel()
            try:
                await identity
            except asyncio.CancelledError:
                pass
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.25, f"identity wait did not happen (elapsed {elapsed:.2f}s)"
        assert elapsed < 5.0, f"identity wait not bounded (elapsed {elapsed:.2f}s)"
        assert s._turn_active is False, "turn must finish after the bounded wait"

    asyncio.run(run())


def test_run_starts_asr_before_identity_finishes(monkeypatch):
    """ASR must start while identity is still loading (no serialization)."""

    async def run():
        s, ws = _make_session()
        s.user = types.SimpleNamespace(id="u-test", username="test")
        s.conversation_id = "c-test"

        async def _noop(*a, **k):
            return None

        monkeypatch.setattr(s, "_ensure_conversation", _noop)
        monkeypatch.setattr(s, "_load_history", _noop)

        identity_started = asyncio.Event()

        async def _slow_identity():
            identity_started.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(s, "_load_identity", _slow_identity)

        state = {"asr_constructed_while_identity_pending": False}

        class _FakeVoiceASR:
            def __init__(self, *a, **k):
                pass

            async def start(self):
                state["asr_constructed_while_identity_pending"] = not identity_started.is_set()

            async def close(self):
                pass

        class _FakeASRService:
            enabled = True

        monkeypatch.setattr(voice_service, "ASRService", lambda: _FakeASRService())
        monkeypatch.setattr(voice_service, "_VoiceASR", _FakeVoiceASR)

        async def _fake_receive_loop():
            # End the session right away — run() unwinds into _shutdown().
            return

        monkeypatch.setattr(s, "_receive_loop", _fake_receive_loop)

        await asyncio.wait_for(s.run(), timeout=15)

        # If identity is still pending when ASR started, the concurrency fix
        # is in place. (With the pre-fix serial order, _load_identity would
        # still be sleeping when run() awaited it — never reaching ASR.)
        assert s._asr is not None, "ASR was never constructed"
        assert s._identity_task is not None, "identity load was not a background task"
        assert state["asr_constructed_while_identity_pending"] is True, (
            "ASR started only after identity finished (serialized startup)"
        )

    asyncio.run(run())
