"""R3 adversarial checks for independent notification delivery state."""
import asyncio

from test_voice_notice_lifecycle import _session, _DummyTTS, _noop


def test_two_notifications_keep_independent_completion_state():
    async def run():
        s = _session()
        consumer = asyncio.create_task(s._tts_consumer())
        try:
            for notification_id in ("N1", "N2"):
                await s._tts_queue.put({"text": notification_id, "epoch": 1, "notification_id": notification_id})
                await s._tts_queue.put({
                    "flush": None,
                    "notification_id": notification_id,
                    "notification_complete": True,
                })
            await asyncio.sleep(0.05)
        finally:
            s._closed = True
            s._interrupt.set()
            await s._tts_queue.put(None)
            await asyncio.gather(consumer, return_exceptions=True)
        for notification_id in ("N1", "N2"):
            state = s._notification_states[notification_id]
            assert state["audio_sent"] is True
            assert state["generation_complete"] is True

    asyncio.run(run())


def test_early_ack_requests_client_rearm_without_state_damage():
    async def run():
        s = _session()
        s._speaking = True
        s._active_notification_id = "N1"
        s._active_notification_audio_sent = True
        s._active_notification_generation_complete = False
        await s._handle_client_event({"event": "playback_drained", "notification_id": "N1"})
        assert s._speaking is True
        assert any(ev.get("event") == "notification_ack_pending" and ev.get("notification_id") == "N1" for ev in s._events)
        assert not any(ev.get("event") == "speaking_end" for ev in s._events)

    asyncio.run(run())


def test_resume_rebuilds_completed_notification_state_after_final_flush():
    async def run():
        s = _session()
        s._current_notice_id = "N1"
        s._notification_states["N1"] = {
            "audio_sent": True,
            "generation_complete": True,
            "tts_failed": False,
            "delivered": False,
            "llm_succeeded": True,
        }
        s._active_notification_id = "N1"
        s._active_notification_audio_sent = True
        s._active_notification_generation_complete = True
        s._playback_paused = True
        s._turn_reply_text = "第一句。第二句。"
        s._paused_spoken_chars = 0
        s._resume_epoch = s._turn_epoch
        s._resume_text = s._turn_reply_text
        s._resume_q = asyncio.Queue()
        await s._resume_q.put(b"\x00\x00")
        await s._resume_q.put(None)
        await s._resume_playback()
        state = s._notification_states["N1"]
        assert state["audio_sent"] is False
        assert state["generation_complete"] is False

        consumer = asyncio.create_task(s._tts_consumer())
        await asyncio.sleep(0.05)
        assert state["generation_complete"] is True
        s._closed = True
        s._interrupt.set()
        await s._tts_queue.put(None)
        await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(run())


def test_tts_failure_is_recorded_per_notification():
    async def run():
        class _FailingTTS:
            async def stream_tts(self, text, style_instruction=None, timeout=60.0):
                raise RuntimeError("tts failed")
                yield b""

        s = _session(tts=_FailingTTS())
        consumer = asyncio.create_task(s._tts_consumer())
        try:
            await s._tts_queue.put({"text": "通知", "epoch": 1, "notification_id": "N1"})
            await asyncio.sleep(0.05)
        finally:
            s._closed = True
            s._interrupt.set()
            await s._tts_queue.put(None)
            await asyncio.gather(consumer, return_exceptions=True)
        assert s._notification_states["N1"]["tts_failed"] is True
        assert s._notification_states["N1"]["generation_complete"] is False

    asyncio.run(run())


def test_stale_ack_pcm_is_dropped_at_consumer():
    async def run():
        s = _session()
        sent = []

        async def _send_bytes(data):
            sent.append(data)

        s._send_bytes = _send_bytes
        s._latest_input_seq = 2
        consumer = asyncio.create_task(s._tts_consumer())
        try:
            await s._tts_queue.put({"text": "好的。", "epoch": 1, "input_seq": 1})
            await asyncio.sleep(0.05)
        finally:
            s._closed = True
            s._interrupt.set()
            await s._tts_queue.put(None)
            await asyncio.gather(consumer, return_exceptions=True)
        assert sent == []

    asyncio.run(run())


def test_late_ack_does_not_clear_newer_active_notification():
    async def run():
        s = _session()
        s._speaking = True
        s._active_notification_id = "N2"
        for notification_id in ("N1", "N2"):
            s._notification_states[notification_id] = {
                "audio_sent": True,
                "generation_complete": True,
                "tts_failed": False,
                "delivered": False,
            }
        await s._handle_client_event({"event": "playback_drained", "notification_id": "N1"})
        assert s._notification_states["N1"]["delivered"] is True
        assert s._notification_states["N2"]["delivered"] is False
        assert s._active_notification_id == "N2"
        assert s._speaking is True
        assert not any(ev.get("event") == "speaking_end" for ev in s._events)

        s._events.clear()
        await s._handle_client_event({"event": "playback_drained", "notification_id": "N1"})
        assert s._active_notification_id == "N2"
        assert s._speaking is True
        assert not any(ev.get("event") == "speaking_end" for ev in s._events)

    asyncio.run(run())


def test_plain_playback_drain_does_not_require_notification_state():
    async def run():
        s = _session()
        s._speaking = True
        s._active_notification_id = "N1"
        await s._handle_client_event({"event": "playback_drained"})
        assert s._speaking is False

    asyncio.run(run())


def test_resume_uses_active_notification_after_generation_context_cleared():
    async def run():
        s = _session()
        s._current_notice_id = None
        s._active_notification_id = "N1"
        s._playback_paused = True
        s._turn_reply_text = "第一句。第二句。"
        s._paused_spoken_chars = 0
        s._resume_epoch = s._turn_epoch
        s._resume_text = s._turn_reply_text
        s._resume_q = asyncio.Queue()
        await s._resume_q.put(None)
        await s._resume_playback()
        item = await s._tts_queue.get()
        assert item.get("notification_id") == "N1", item

    asyncio.run(run())


def test_tts_failure_releases_queued_id_even_with_successful_llm_flush():
    async def run():
        s = _session()
        s._current_notice_id = "N1"
        s._queued_notice_ids.add("N1")
        s._notification_states["N1"] = {
            "audio_sent": False,
            "generation_complete": True,
            "tts_failed": True,
            "delivered": False,
        }
        s._active_notification_generation_complete = True

        async def _fail_during_generation(*args, **kwargs):
            s._notification_states["N1"]["tts_failed"] = True

        s._generate_and_speak = _fail_during_generation
        await s._deliver_bg_task_notice({"task_id": "N1", "title": "任务", "status": "completed"}, notification_id="N1")
        assert "N1" not in s._queued_notice_ids

    asyncio.run(run())


def test_second_notice_waits_for_previous_ack_before_generation():
    async def run():
        s = _session()
        s._active_notification_id = "N1"
        s._notification_states["N1"] = {
            "audio_sent": True,
            "generation_complete": True,
            "tts_failed": False,
            "delivered": False,
        }
        called = []

        async def _generate(text, **kwargs):
            called.append(text)

        s._generate_and_speak = _generate
        await s._deliver_bg_task_notice({"task_id": "N2", "title": "第二件", "status": "completed"}, notification_id="N2")
        assert called == [], "N1 未实际播完前不得开始 N2"
        item = s._turn_queue._queue[0]
        assert item["notification_id"] == "N2" and item.get("retry_at"), item
        s._notification_states["N1"]["delivered"] = True
        responder = asyncio.create_task(s._responder())
        try:
            await asyncio.sleep(0.35)
        finally:
            responder.cancel()
            await asyncio.gather(responder, return_exceptions=True)
        assert called, "N1 已播完后才允许 N2"

    asyncio.run(run())


def test_zero_audio_releases_queued_id_for_retry():
    async def run():
        s = _session()
        s._current_notice_id = "N1"
        s._queued_notice_ids.add("N1")
        s._notification_states["N1"] = {
            "audio_sent": False,
            "generation_complete": True,
            "tts_failed": False,
            "delivered": False,
        }
        s._active_notification_generation_complete = True
        s._generate_and_speak = _noop
        await s._deliver_bg_task_notice({"task_id": "N1", "title": "任务", "status": "completed"}, notification_id="N1")
        assert "N1" not in s._queued_notice_ids

    asyncio.run(run())


def test_failed_notice_ack_is_terminal_not_rearm_loop():
    async def run():
        s = _session()
        s._speaking = True
        s._active_notification_id = "N1"
        s._active_notification_audio_sent = True
        s._active_notification_generation_complete = True
        s._notification_states["N1"] = {
            "audio_sent": True,
            "generation_complete": True,
            "tts_failed": True,
            "delivered": False,
        }
        await s._handle_client_event({"event": "playback_drained", "notification_id": "N1"})
        assert s._speaking is False
        assert any(ev.get("event") == "notification_failed" and ev.get("notification_id") == "N1" for ev in s._events)
        assert not any(ev.get("event") == "notification_ack_pending" for ev in s._events)

    asyncio.run(run())


def test_first_chunk_timeout_marks_tts_failure(monkeypatch):
    async def run():
        class _HangingTTS:
            async def stream_tts(self, text, style_instruction=None, timeout=60.0):
                await asyncio.sleep(60)
                yield b""

        async def _timeout(*args, **kwargs):
            raise asyncio.TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", _timeout)
        s = _session(tts=_HangingTTS())
        consumer = asyncio.create_task(s._tts_consumer())
        try:
            await s._tts_queue.put({"text": "通知", "epoch": 1, "notification_id": "N1"})
            await asyncio.sleep(0.05)
        finally:
            s._closed = True
            s._interrupt.set()
            await s._tts_queue.put(None)
            await asyncio.gather(consumer, return_exceptions=True)
        assert s._notification_states["N1"]["tts_failed"] is True

    asyncio.run(run())


def test_late_failed_ack_does_not_stop_newer_playback():
    async def run():
        s = _session()
        s._speaking = True
        s._active_notification_id = "N2"
        s._notification_states["N1"] = {
            "audio_sent": True,
            "generation_complete": True,
            "tts_failed": True,
            "delivered": False,
        }
        s._notification_states["N2"] = {
            "audio_sent": True,
            "generation_complete": True,
            "tts_failed": False,
            "delivered": False,
        }
        await s._handle_client_event({"event": "playback_drained", "notification_id": "N1"})
        assert s._active_notification_id == "N2"
        assert s._speaking is True
        assert not any(ev.get("event") == "speaking_end" for ev in s._events)
        assert not any(ev.get("event") == "notification_failed" for ev in s._events)

    asyncio.run(run())


def test_zero_audio_releases_active_ownership_for_next_notice():
    async def run():
        class _EmptyTTS:
            async def stream_tts(self, text, style_instruction=None, timeout=60.0):
                return
                yield b""

        s = _session(tts=_EmptyTTS())
        consumer = asyncio.create_task(s._tts_consumer())
        try:
            await s._tts_queue.put({"text": "失败通知", "epoch": 1, "notification_id": "N1"})
            await asyncio.sleep(0.05)
        finally:
            s._closed = True
            s._interrupt.set()
            await s._tts_queue.put(None)
            await asyncio.gather(consumer, return_exceptions=True)
        assert s._active_notification_id is None
        assert s._notification_states["N1"]["tts_failed"] is True
        assert any(ev.get("event") == "notification_failed" and ev.get("notification_id") == "N1" for ev in s._events)

    asyncio.run(run())


def test_pause_before_first_chunk_is_not_tts_failure():
    async def run():
        s = _session()

        class _PauseTTS:
            async def stream_tts(self, text, style_instruction=None, timeout=60.0):
                await s._pause_playback()
                return
                yield b""

        s.tts = _PauseTTS()
        consumer = asyncio.create_task(s._tts_consumer())
        try:
            await s._tts_queue.put({"text": "通知", "epoch": 1, "notification_id": "N1"})
            await asyncio.sleep(0.05)
        finally:
            s._closed = True
            s._interrupt.set()
            await s._tts_queue.put(None)
            await asyncio.gather(consumer, return_exceptions=True)
        assert not s._notification_states["N1"]["tts_failed"]
        assert not any(ev.get("event") == "notification_failed" for ev in s._events)

    asyncio.run(run())


def test_plain_drain_releases_active_for_retry_without_delivery():
    async def run():
        s = _session()
        s._speaking = True
        s._active_notification_id = "N1"
        s._active_notification_audio_sent = True
        s._active_notification_generation_complete = True
        s._queued_notice_ids.add("N1")
        s._notification_states["N1"] = {
            "audio_sent": True,
            "generation_complete": True,
            "tts_failed": False,
            "delivered": False,
        }
        await s._handle_client_event({"event": "playback_drained"})
        assert s._speaking is False
        assert s._active_notification_id is None
        assert "N1" not in s._queued_notice_ids
        assert s._notification_states["N1"]["delivered"] is False

    asyncio.run(run())


def test_partial_audio_failure_waits_for_drain_before_release():
    async def run():
        class _PartialFailTTS:
            async def stream_tts(self, text, style_instruction=None, timeout=60.0):
                yield b"\x00" * 960
                await asyncio.sleep(0.02)  # 让消费者先发出首块，再发生中途失败
                raise RuntimeError("mid-stream failure")

        s = _session(tts=_PartialFailTTS())
        s._speaking = True
        consumer = asyncio.create_task(s._tts_consumer())
        try:
            await s._tts_queue.put({"text": "通知", "epoch": 1, "notification_id": "N1"})
            await asyncio.sleep(0.05)
            assert s._active_notification_id == "N1"
            assert s._speaking is True
            assert s._notification_states["N1"]["tts_failed"] is True
        finally:
            s._closed = True
            s._interrupt.set()
            await s._tts_queue.put(None)
            await asyncio.gather(consumer, return_exceptions=True)
        await s._handle_client_event({"event": "playback_drained"})
        assert s._active_notification_id is None
        assert s._notification_states["N1"]["delivered"] is False

    asyncio.run(run())


def test_empty_pcm_frame_releases_active_ownership():
    async def run():
        class _EmptyFrameTTS:
            async def stream_tts(self, text, style_instruction=None, timeout=60.0):
                yield b""

        s = _session(tts=_EmptyFrameTTS())
        consumer = asyncio.create_task(s._tts_consumer())
        try:
            await s._tts_queue.put({"text": "通知", "epoch": 1, "notification_id": "N1"})
            await asyncio.sleep(0.05)
        finally:
            s._closed = True
            s._interrupt.set()
            await s._tts_queue.put(None)
            await asyncio.gather(consumer, return_exceptions=True)
        assert s._active_notification_id is None
        assert s._notification_states["N1"]["audio_sent"] is False
        assert s._notification_states["N1"]["tts_failed"] is True

    asyncio.run(run())


def test_resume_restores_identity_and_completion_marker():
    async def run():
        s = _session()
        s._current_notice_id = "N1"
        s._active_notification_id = "N1"
        s._notification_states["N1"] = {
            "audio_sent": True,
            "generation_complete": False,
            "llm_succeeded": True,
            "tts_failed": False,
            "delivered": False,
        }
        s._playback_paused = True
        s._turn_reply_text = "第一句。第二句。"
        s._paused_spoken_chars = 0
        s._resume_epoch = s._turn_epoch
        s._resume_text = s._turn_reply_text
        s._resume_q = asyncio.Queue()
        await s._resume_q.put(None)
        await s._resume_playback()
        items = []
        while not s._tts_queue.empty():
            items.append(await s._tts_queue.get())
        text_items = [item for item in items if isinstance(item, dict) and item.get("text")]
        assert all(item.get("notification_id") == "N1" for item in text_items), text_items
        flush = next(item for item in items if isinstance(item, dict) and "flush" in item)
        assert flush.get("notification_id") == "N1"
        assert flush.get("notification_complete") is True
        assert s._notification_states["N1"]["delivered"] is False
        assert any(ev.get("event") == "playback_resumed" and ev.get("notification_id") == "N1" and ev.get("has_remaining") is True for ev in s._events)

    asyncio.run(run())


def test_resume_does_not_complete_failed_generation():
    async def run():
        s = _session()
        s._current_notice_id = None
        s._active_notification_id = "N1"
        s._notification_states["N1"] = {
            "audio_sent": True,
            "generation_complete": False,
            "llm_succeeded": False,
            "tts_failed": False,
            "delivered": False,
        }
        s._playback_paused = True
        s._turn_reply_text = "半截通知。"
        s._paused_spoken_chars = 0
        s._resume_epoch = s._turn_epoch
        s._resume_text = s._turn_reply_text
        s._resume_q = asyncio.Queue()
        await s._resume_q.put(None)
        await s._resume_playback()
        assert s._notification_states["N1"]["generation_complete"] is False
        assert not any(isinstance(item, dict) and item.get("notification_complete") for item in s._tts_queue._queue)

    asyncio.run(run())


def test_zero_remaining_resume_restores_completion(monkeypatch):
    async def run():
        class _DBContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        async def _settled(_db, _notification_id, _delivery_attempt_id=None):
            return True

        monkeypatch.setattr("app.db.database.AsyncSessionLocal", lambda: _DBContext())
        monkeypatch.setattr(
            "app.services.voice_notification_service.mark_voice_notification_delivered",
            _settled,
        )
        s = _session()
        s._current_notice_id = None
        s._active_notification_id = "N1"
        s._notification_states["N1"] = {
            "audio_sent": True,
            "generation_complete": False,
            "llm_succeeded": True,
            "tts_failed": False,
            "delivered": False,
        }
        s._playback_paused = True
        s._turn_reply_text = "已经播完。"
        s._paused_spoken_chars = len(s._turn_reply_text)
        await s._resume_playback()
        assert s._notification_states["N1"]["delivered"] is True
        assert s._active_notification_id is None
        assert any(ev.get("event") == "playback_resumed" and ev.get("notification_id") == "N1" and ev.get("has_remaining") is False for ev in s._events)

    asyncio.run(run())


def test_plain_speak_text_does_not_inherit_active_notification():
    async def run():
        s = _session()
        s._active_notification_id = "N1"
        s._current_notice_id = None
        from app.services.voice_tts import VoiceTTSMixin
        await VoiceTTSMixin._speak_text(s, "普通用户回答", wait=False)
        item = await s._tts_queue.get()
        assert item.get("notification_id") is None, item

    asyncio.run(run())


def test_ordinary_resume_does_not_inherit_active_notification():
    async def run():
        s = _session()
        s._active_notification_id = "N1"
        s._current_notice_id = None
        s._playback_paused = True
        s._turn_reply_text = "普通用户回答。"
        s._paused_spoken_chars = 0
        s._resume_epoch = s._turn_epoch
        s._resume_text = s._turn_reply_text
        s._resume_q = asyncio.Queue()
        await s._resume_q.put(None)
        # 普通音频项会把 active 播放归属清空，但保留 N1 的独立 outbox 状态。
        s._active_notification_id = None
        await s._resume_playback()
        item = await s._tts_queue.get()
        assert item.get("notification_id") is None, item
        resumed = next(ev for ev in s._events if ev.get("event") == "playback_resumed")
        assert resumed.get("notification_id") is None, resumed

    asyncio.run(run())


def test_resume_ack_requires_current_burst_seq():
    async def run():
        s = _session()
        s._active_notification_id = "N1"
        s._active_notification_audio_sent = True
        s._active_notification_generation_complete = True
        s._notification_states["N1"] = {
            "audio_sent": True,
            "generation_complete": True,
            "tts_failed": False,
            "delivered": False,
            "burst_seq": 1,
        }
        await s._handle_client_event({"event": "playback_drained", "notification_id": "N1", "notice_burst_seq": 0})
        assert not s._notification_states["N1"]["delivered"], "pre-pause 的旧 burst ACK 不得结算"
        await s._handle_client_event({"event": "playback_drained", "notification_id": "N1", "notice_burst_seq": 1})
        assert s._notification_states["N1"]["delivered"] is True

    asyncio.run(run())


def test_resume_event_precedes_audio_and_carries_burst_seq():
    async def run():
        s = _session()
        s._active_notification_id = "N1"
        s._current_notice_id = "N1"
        s._notification_states["N1"] = {
            "audio_sent": True,
            "generation_complete": False,
            "llm_succeeded": True,
            "tts_failed": False,
            "delivered": False,
            "burst_seq": 0,
        }
        s._playback_paused = True
        s._turn_reply_text = "第一句。第二句。"
        s._paused_spoken_chars = 0
        s._resume_epoch = s._turn_epoch
        s._resume_text = s._turn_reply_text
        s._resume_q = asyncio.Queue()
        await s._resume_q.put(b"\x00" * 960)
        await s._resume_q.put(None)
        consumer = asyncio.create_task(s._tts_consumer())
        try:
            await s._resume_playback()
            await asyncio.sleep(0.05)
            resumed = next(ev for ev in s._events if ev.get("event") == "playback_resumed")
            assert resumed.get("notice_burst_seq") == 1, resumed
            assert resumed.get("has_remaining") is True
            assert s._events.index(resumed) < next(i for i, ev in enumerate(s._events) if ev.get("event") == "speaking_start")
        finally:
            s._closed = True
            s._interrupt.set()
            await s._tts_queue.put(None)
            await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(run())


def test_ordinary_audio_emits_answer_start_to_clear_client_notice():
    async def run():
        s = _session()
        s._speaking = True
        s._active_notification_id = "N1"
        consumer = asyncio.create_task(s._tts_consumer())
        try:
            await s._tts_queue.put({"text": "普通回答", "epoch": 1})
            await asyncio.sleep(0.05)
        finally:
            s._closed = True
            s._interrupt.set()
            await s._tts_queue.put(None)
            await asyncio.gather(consumer, return_exceptions=True)
        assert any(ev.get("event") == "speaking_start" and ev.get("notification_id") is None for ev in s._events), s._events

    asyncio.run(run())


def test_late_incomplete_ack_releases_notice_for_replay():
    async def run():
        s = _session()
        s._active_notification_id = None  # 普通回答正在播放
        s._queued_notice_ids.add("N1")
        s._notification_states["N1"] = {
            "audio_sent": True,
            "generation_complete": False,
            "tts_failed": False,
            "delivered": False,
            "burst_seq": 0,
        }
        await s._handle_client_event({
            "event": "playback_drained", "notification_id": "N1", "notice_burst_seq": 0,
        })
        assert s._notification_states["N1"]["delivered"] is False
        assert "N1" not in s._queued_notice_ids
        await s._enqueue_bg_task_notice({"task_id": "N1"}, notification_id="N1")
        assert s._turn_queue.qsize() == 1

    asyncio.run(run())


def test_interrupt_releases_orphaned_notice_for_replay():
    async def run():
        s = _session()
        s._active_notification_id = None  # N1 已切到普通回答，只保留在前端 drain map
        s._current_notice_id = None
        s._queued_notice_ids.add("N1")
        s._notification_states["N1"] = {
            "audio_sent": True,
            "generation_complete": True,
            "tts_failed": False,
            "delivered": False,
            "burst_seq": 0,
        }
        await s._request_interrupt()
        assert s._notification_states["N1"]["delivered"] is False
        assert "N1" not in s._queued_notice_ids
        await s._enqueue_bg_task_notice({"task_id": "N1"}, notification_id="N1")
        assert s._turn_queue.qsize() == 1

    asyncio.run(run())


def test_pause_releases_only_nonresumable_notice_latches():
    async def run():
        s = _session()
        s._speaking = True
        s._active_notification_id = "N2"
        s._current_notice_id = "N2"
        s._queued_notice_ids.update({"N1", "N2"})
        for notice_id in ("N1", "N2"):
            s._notification_states[notice_id] = {
                "audio_sent": True,
                "generation_complete": True,
                "tts_failed": False,
                "delivered": False,
                "burst_seq": 0,
            }
        await s._pause_playback()
        assert "N1" not in s._queued_notice_ids
        assert "N2" in s._queued_notice_ids
        assert s._notification_states["N1"]["delivered"] is False

    asyncio.run(run())


def test_cancellation_preserves_unstarted_notice_latch():
    async def run():
        for action in ("interrupt", "pause"):
            s = _session()
            s._speaking = True
            s._active_notification_id = None
            s._current_notice_id = None
            s._queued_notice_ids.add("N3")
            s._notification_states["N3"] = {
                "audio_sent": False,
                "generation_complete": False,
                "tts_failed": False,
                "delivered": False,
                "burst_seq": 0,
            }
            original = {"notice": {"task_id": "N3"}, "notification_id": "N3"}
            await s._turn_queue.put(original)
            if action == "interrupt":
                await s._request_interrupt()
            else:
                await s._pause_playback()
            assert "N3" in s._queued_notice_ids, action
            await s._enqueue_bg_task_notice({"task_id": "N3"}, notification_id="N3")
            assert s._turn_queue.qsize() == 1, action

    asyncio.run(run())


def test_older_notification_can_ack_after_newer_one_started():
    async def run():
        s = _session()
        s._current_notice_id = "N2"
        s._active_notification_id = "N2"
        for notification_id in ("N1", "N2"):
            s._notification_states[notification_id] = {
                "audio_sent": True,
                "generation_complete": True,
                "tts_failed": False,
                "delivered": False,
            }
        await s._handle_client_event({"event": "playback_drained", "notification_id": "N1"})
        assert s._notification_states["N1"]["delivered"] is True
        assert not any(ev.get("event") == "notification_ack_pending" for ev in s._events)

    asyncio.run(run())
