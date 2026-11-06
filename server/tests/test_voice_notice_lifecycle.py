"""Adversarial lifecycle tests for durable voice notification delivery."""
import asyncio
from types import SimpleNamespace

from app.services.voice_service import VoiceDuplexSession
from app.services import voice_service as vs
from app.db import database as db_mod
from test_voice_notification_outbox import _make_db, _seed, _noop, _orm


class _DummyTTS:
    async def stream_tts(self, text, style_instruction=None, timeout=60.0):
        yield b"\x00" * 960


class _ErrorLLM:
    async def stream_chat_structured(self, messages, **kwargs):
        yield {"type": "content", "data": "只生成了一半"}
        yield {"type": "error", "data": "provider failed"}


def _session(tts=None):
    s = VoiceDuplexSession(None, None, None, conversation_id=None)
    s.tts = tts or _DummyTTS()
    s._events = []
    s._persisted = []

    async def _send_json(ev):
        s._events.append(ev)

    async def _persist(role, content, **kwargs):
        s._persisted.append((role, content))

    async def _speak_text(text, wait=True, aux=False, **kwargs):
        return None

    s._send_json = _send_json
    s._persist_message = _persist
    s._speak_text = _speak_text
    s._set_state = _noop
    s._schedule_filler_ttft_watch = lambda: None
    s._clear_pending_filler = lambda: None
    s._extra_body = lambda svc=None: {}
    s._build_llm = lambda purpose="voice": (_ErrorLLM(), "fake")
    return s


def test_resume_preserves_notification_identity():
    async def run():
        s = _session()
        s._current_notice_id = "notice-N"
        s._active_notification_id = "notice-N"  # paused after first PCM, not preparing
        s._playback_paused = True
        s._turn_reply_text = "第一句。第二句。"
        s._paused_spoken_chars = 0
        s._resume_epoch = s._turn_epoch
        s._resume_text = s._turn_reply_text
        s._resume_q = asyncio.Queue()
        await s._resume_q.put(None)
        await s._resume_playback()
        item = await s._tts_queue.get()
        assert item.get("notification_id") == "notice-N", item

    asyncio.run(run())


def test_intermediate_speak_text_flush_is_not_generation_completion():
    async def run():
        s = _session()
        s._current_notice_id = "notice-N"
        # Bypass the test double and inspect the real queue implementation.
        from app.services.voice_tts import VoiceTTSMixin

        items = []

        async def drain():
            while True:
                item = await s._tts_queue.get()
                items.append(item)
                if isinstance(item, dict) and "flush" in item:
                    item["flush"].set_result(True)

        task = asyncio.create_task(drain())
        try:
            await VoiceTTSMixin._speak_text(s, "我来调用工具", wait=True)
        finally:
            task.cancel()
        flush = next(item for item in items if isinstance(item, dict) and "flush" in item)
        assert not flush.get("notification_complete", False), flush

    asyncio.run(run())


def test_partial_error_flush_is_explicitly_not_complete():
    async def run():
        s = _session()
        s._current_notice_id = "notice-N"
        items = []

        async def drain():
            while True:
                item = await s._tts_queue.get()
                if item is None:
                    return
                items.append(item)
                if isinstance(item, dict) and "flush" in item:
                    item["flush"].set_result(True)

        task = asyncio.create_task(drain())
        try:
            await s._generate_and_speak_impl("通知", allow_filler=False, user_turn=False)
        finally:
            task.cancel()
        flush = next(item for item in items if isinstance(item, dict) and "flush" in item)
        assert flush.get("notification_complete") is False, flush

    asyncio.run(run())


def test_contiguous_notice_emits_new_speaking_start():
    async def run():
        s = _session()
        s._speaking = True
        consumer = asyncio.create_task(s._tts_consumer())
        try:
            await s._tts_queue.put({"text": "通知", "epoch": 1, "notification_id": "notice-N"})
            await asyncio.sleep(0.05)
        finally:
            s._closed = True
            s._interrupt.set()
            await s._tts_queue.put(None)
            await asyncio.gather(consumer, return_exceptions=True)
        assert any(ev.get("event") == "speaking_start" and ev.get("notification_id") == "notice-N" for ev in s._events), s._events

    asyncio.run(run())


def test_wrong_ack_does_not_end_current_playback():
    async def run():
        s = _session()
        s._speaking = True
        s._active_notification_id = "notice-current"
        s._active_notification_audio_sent = True
        s._active_notification_generation_complete = True
        await s._handle_client_event({"event": "playback_drained", "notification_id": "notice-stale"})
        assert s._speaking is True
        assert not any(ev.get("event") == "speaking_end" for ev in s._events)

    asyncio.run(run())


def test_pre_tts_interrupt_releases_notice_for_retry():
    async def run():
        s = _session()
        s._current_notice_id = "notice-N"
        s._queued_notice_ids.add("notice-N")
        await s._request_interrupt()
        assert "notice-N" not in s._queued_notice_ids

    asyncio.run(run())


def test_duplicate_live_hook_respects_recent_claim(monkeypatch):
    async def run():
        engine, factory = _make_db()
        try:
            uid, cid, _, task_id = await _seed(factory, monkeypatch, voice=True)
            from app.services.voice_notification_service import claim_voice_notifications
            async with factory() as db:
                task = await db.get(db_mod.AgentTask, task_id)
                from app.services.voice_notification_service import record_voice_task_notification
                await record_voice_task_notification(db, task)
                claimed = await claim_voice_notifications(db, uid, cid)
                assert len(claimed) == 1
            calls = []

            class _Session:
                conversation_id = cid
                _closed = False

                async def _enqueue_bg_task_notice(self, info, notification_id=None):
                    calls.append(notification_id)

            monkeypatch.setattr(vs, "_ACTIVE_VOICE_SESSIONS", {uid: _Session()})
            assert await vs.notify_voice_task_finished(task_id) is False
            assert calls == []
        finally:
            engine.dispose()

    asyncio.run(run())


def test_prefetched_summary_consumes_only_when_prompt_was_seen():
    async def run():
        s = _session()

        async def no_delegation_claim(_text):
            return False

        s._claims_delegation_success = no_delegation_claim
        s._pending_tool_summary_note = "owed"
        s._prefetch_summary_note = "owed"
        await s._generate_and_speak_impl("新问题", prefetched="结果已经查清。", allow_filler=False)
        assert s._pending_tool_summary_note == ""

        s._pending_tool_summary_note = "owed-again"
        s._prefetch_summary_note = "older-context"
        await s._generate_and_speak_impl("新问题", prefetched="另一条回答。", allow_filler=False)
        assert s._pending_tool_summary_note == "owed-again"

    asyncio.run(run())
