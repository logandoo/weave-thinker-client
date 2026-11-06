"""Regression tests for turn-owned voice audio and canned-aux removal."""
import asyncio
import base64
import json
import inspect

from app.services.voice_service import VoiceDuplexSession, _split_segments


class _DummyWS:
    def __init__(self):
        self.binary = []
        self.text = []

    async def send_bytes(self, data):
        self.binary.append(data)

    async def send_text(self, data):
        self.text.append(data)


class _FakeLLM:
    client = type("Client", (), {"base_url": "https://example.invalid/v1"})()

    async def stream_chat_structured(self, _messages, **_kwargs):
        yield {"type": "content", "data": "这是直接回答。"}


def _make_session():
    ws = _DummyWS()
    session = VoiceDuplexSession(ws, None, None, conversation_id=None)
    return session, ws


def test_pcm_is_sent_as_turn_owned_monotonic_audio_events():
    async def run():
        session, ws = _make_session()
        session._speaking_epoch = 7
        session._audio_protocol = "epoch_v1"
        await session._send_bytes(b"\x01\x00\x02\x00")
        await session._send_bytes(b"\x03\x00")

        assert not ws.binary, "v2 audio must carry metadata instead of anonymous binary PCM"
        events = [json.loads(item) for item in ws.text]
        assert [event.get("event") for event in events] == ["audio_chunk", "audio_chunk"]
        assert [event.get("turn_epoch") for event in events] == [7, 7]
        assert [event.get("sequence") for event in events] == [1, 2]
        assert base64.b64decode(events[0]["audio"]) == b"\x01\x00\x02\x00"
        assert base64.b64decode(events[1]["audio"]) == b"\x03\x00"

    asyncio.run(run())


def test_interrupt_event_names_the_cancelled_turn():
    async def run():
        session, ws = _make_session()
        events = []

        async def capture(event):
            events.append(event)

        session._send_json = capture
        session._turn_epoch = 11
        await session._request_interrupt()
        interrupted = [event for event in events if event.get("event") == "interrupted"]
        assert interrupted and interrupted[-1].get("turn_epoch") == 11

    asyncio.run(run())


def test_normal_answer_never_schedules_canned_filler():
    async def run():
        session, ws = _make_session()

        def forbidden_filler():
            raise AssertionError("normal answer entered canned filler watch")

        async def noop(*_args, **_kwargs):
            return None

        session._schedule_filler_ttft_watch = forbidden_filler
        session._persist_message = noop
        session._set_state = noop
        session._extra_body = lambda _svc=None: {}
        session._build_llm = lambda purpose="voice": (_FakeLLM(), "fake")

        async def drain():
            while True:
                item = await session._tts_queue.get()
                if item is None:
                    return
                if isinstance(item, dict) and "flush" in item:
                    future = item["flush"]
                    if future and not future.done():
                        future.set_result(True)

        consumer = asyncio.create_task(drain())
        try:
            await session._generate_and_speak_impl("直接回答", allow_filler=True)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        assert not [event for event in ws.text if json.loads(event).get("event") == "filler"]

    asyncio.run(run())


def test_eot_watchdog_does_not_emit_static_backchannel():
    async def run():
        session, _ws = _make_session()

        async def forbidden_backchannel(_silence, _text):
            raise AssertionError("EOT watchdog entered static backchannel generation")

        session._maybe_backchannel = forbidden_backchannel
        session._pending_turn_text = "用户还在说话"
        session._asr_ready = True
        task = asyncio.create_task(session._eot_watchdog())
        await asyncio.sleep(0.15)
        session._closed = True
        await asyncio.gather(task, return_exceptions=True)
        assert not task.done() or task.exception() is None

    asyncio.run(run())


def test_epoch_handoff_emits_start_while_previous_tail_is_playing():
    class OneChunkTTS:
        async def stream_tts(self, _text, style_instruction=None, timeout=60.0):
            yield b"\x00\x00" * 16

    async def run():
        session, _ws = _make_session()
        session.tts = OneChunkTTS()
        events = []

        async def capture(event):
            events.append(event)

        session._send_json = capture
        consumer = asyncio.create_task(session._tts_consumer())
        await session._tts_queue.put({"text": "旧回答", "epoch": 1})
        await session._tts_queue.put({"text": "新回答", "epoch": 2})
        await asyncio.sleep(0.1)
        starts = [event for event in events if event.get("event") == "speaking_start"]
        assert [event.get("turn_epoch") for event in starts] == [1, 2]
        session._closed = True
        session._interrupt.set()
        await session._tts_queue.put(None)
        await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(run())


def test_direct_utterance_after_interrupt_gets_new_epoch():
    async def run():
        session, _ws = _make_session()
        session._turn_epoch = 4
        session._interrupt.set()
        await session._speak_direct_text("取消后的新通知", wait=False)
        items = [item for item in list(session._tts_queue._queue) if isinstance(item, dict) and item.get("text")]
        assert items and items[0].get("epoch") == 5
        assert not session._interrupt.is_set()

    asyncio.run(run())


def test_audio_protocol_negotiation_preserves_legacy_clients():
    async def run():
        legacy, legacy_ws = _make_session()
        await legacy._send_bytes(b"\x01\x00")
        assert legacy_ws.binary == [b"\x01\x00"]
        assert not legacy_ws.text

        modern, modern_ws = _make_session()
        modern._audio_protocol = "epoch_v1"
        modern._speaking_epoch = 1
        await modern._send_bytes(b"\x01\x00")
        assert not modern_ws.binary
        assert json.loads(modern_ws.text[0])["event"] == "audio_chunk"

    asyncio.run(run())


def test_direct_speech_allocates_one_positive_epoch_for_all_segments():
    async def run():
        session, _ws = _make_session()
        await session._speak_text("第一句。第二句！", wait=False)
        items = [item for item in list(session._tts_queue._queue) if isinstance(item, dict) and item.get("text")]
        assert items
        assert len({item.get("epoch") for item in items}) == 1
        assert items[0]["epoch"] > 0

    asyncio.run(run())


def test_generation_cancel_event_names_the_cancelled_turn():
    async def run():
        session, _ws = _make_session()
        events = []

        async def capture(event):
            events.append(event)

        async def noop(*_args, **_kwargs):
            return None

        session._send_json = capture
        session._persist_message = noop
        session._set_state = noop

        async def no_claim(_text):
            return False

        async def suppress_stale(*_args, **_kwargs):
            return '{"suppress": true}'

        session._claims_delegation_success = no_claim
        session._quick_classify = suppress_stale
        session._generation_input_seq = 1
        session._latest_input_seq = 2
        await session._generate_and_speak_impl(
            "旧问题", prefetched="好的。", allow_filler=False,
        )
        cancelled = [event for event in events if event.get("event") == "generation_cancelled"]
        assert cancelled and cancelled[-1].get("turn_epoch") == session._turn_epoch

    asyncio.run(run())


def test_stale_playback_progress_cannot_move_current_turn():
    async def run():
        session, _ws = _make_session()
        session._turn_epoch = 5
        await session._handle_client_event({
            "event": "playback_progress",
            "turn_epoch": 4,
            "played_sec": 9.0,
            "total_sec": 10.0,
        })
        assert session._playback_played_sec == 0.0
        assert session._playback_total_sec == 0.0

    asyncio.run(run())


def test_direct_speech_after_normal_drain_gets_new_epoch():
    async def run():
        session, _ws = _make_session()
        session._turn_epoch = 1
        session._completed_speaking_epoch = 1
        session._speaking = False
        await session._speak_direct_text("下一条通知", wait=False)
        items = [item for item in list(session._tts_queue._queue) if isinstance(item, dict) and item.get("text")]
        assert items and items[0].get("epoch") == 2

    asyncio.run(run())


def test_early_flush_does_not_complete_the_epoch():
    async def run():
        session, _ws = _make_session()
        session._turn_epoch = 1
        session._speaking_epoch = 1
        session._speaking = True
        session._turn_audio_sec_total = 1.0
        consumer = asyncio.create_task(session._tts_consumer())
        await session._tts_queue.put({"flush": None, "epoch": 1, "turn_complete": False})
        await asyncio.sleep(0.05)
        await session._handle_client_event({
            "event": "playback_drained", "turn_epoch": 1, "notification_id": None,
        })
        assert session._speaking is True, "early tool/notice flush completed the whole answer"
        await session._tts_queue.put({"flush": None, "epoch": 1, "turn_complete": True})
        await asyncio.sleep(0.05)
        await session._handle_client_event({
            "event": "playback_drained", "turn_epoch": 1, "notification_id": None,
        })
        assert session._speaking is False
        session._closed = True
        await session._tts_queue.put(None)
        await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(run())


def test_live_tool_ack_is_not_a_final_flush():
    async def run():
        session, _ws = _make_session()

        async def noop(*_args, **_kwargs):
            return None

        session._send_json = noop
        session._set_state = noop
        session._turn_active = True
        session._tools_running = True
        session._turn_epoch = 1
        await session._on_user_turn("请继续查询当前结果，并在拿到后告诉我")
        await asyncio.sleep(0.05)
        flushes = [item for item in list(session._tts_queue._queue) if isinstance(item, dict) and "flush" in item]
        assert flushes and all(item.get("turn_complete") is False for item in flushes)

    asyncio.run(run())


def test_prefetched_answer_restores_final_flush_after_pause():
    async def run():
        session, _ws = _make_session()

        async def noop(*_args, **_kwargs):
            return None

        async def no_claim(_text):
            return False

        session._persist_message = noop
        session._set_state = noop
        session._claims_delegation_success = no_claim
        resolved = []

        async def resolve_flushes():
            while True:
                item = await session._tts_queue.get()
                if isinstance(item, dict) and "flush" in item:
                    resolved.append(item)
                    if item["flush"]:
                        item["flush"].set_result(True)

        resolver = asyncio.create_task(resolve_flushes())
        try:
            await session._generate_and_speak_impl(
                "问题", prefetched="完整回答。", allow_filler=False,
            )
            before_resume = len(resolved)
            session._playback_paused = True
            session._turn_reply_text = "完整回答。"
            session._paused_spoken_chars = 2
            await session._resume_playback()
            await asyncio.sleep(0.05)
            resumed = resolved[before_resume:]
            assert any(item.get("turn_complete") is True for item in resumed)
        finally:
            resolver.cancel()
            await asyncio.gather(resolver, return_exceptions=True)

    asyncio.run(run())


def test_resume_restores_a_final_flush_dropped_by_pause():
    async def run():
        session, _ws = _make_session()

        async def noop(*_args, **_kwargs):
            return None

        session._send_json = noop
        session._turn_epoch = 3
        session._playback_paused = True
        session._turn_reply_text = "已播部分。剩余回答。"
        session._paused_spoken_chars = 5
        session._final_flush_enqueued_epoch = 3

        resolved = []

        async def resolve_flushes():
            while True:
                item = await session._tts_queue.get()
                if isinstance(item, dict) and "flush" in item:
                    resolved.append(item)
                    if item["flush"]:
                        item["flush"].set_result(True)

        resolver = asyncio.create_task(resolve_flushes())
        try:
            await session._resume_playback()
        finally:
            resolver.cancel()
            await asyncio.gather(resolver, return_exceptions=True)
        flushes = resolved + [item for item in list(session._tts_queue._queue) if isinstance(item, dict) and "flush" in item]
        assert any(item.get("turn_complete") is True and item.get("epoch") == 3 for item in flushes)

    asyncio.run(run())


def test_plain_drain_waits_for_the_turn_flush():
    async def run():
        session, _ws = _make_session()
        events = []

        async def capture(event):
            events.append(event)

        session._send_json = capture
        session._turn_epoch = 1
        session._speaking_epoch = 1
        session._speaking = True
        session._turn_audio_sec_total = 1.0
        await session._handle_client_event({
            "event": "playback_drained",
            "turn_epoch": 1,
            "notification_id": None,
        })
        assert session._speaking is True, "temporary underrun ended an unfinished turn"
        assert any(event.get("event") == "notification_ack_pending" for event in events)

        consumer = asyncio.create_task(session._tts_consumer())
        await session._tts_queue.put({"flush": None, "epoch": 1, "turn_complete": True})
        await asyncio.sleep(0.05)
        await session._handle_client_event({
            "event": "playback_drained",
            "turn_epoch": 1,
            "notification_id": None,
        })
        assert session._speaking is False
        session._closed = True
        await session._tts_queue.put(None)
        await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(run())


def test_stale_plain_drain_cannot_end_current_turn():
    async def run():
        session, _ws = _make_session()

        async def noop(_event):
            return None

        session._send_json = noop
        session._turn_epoch = 5
        session._speaking_epoch = 5
        session._speaking = True
        await session._handle_client_event({
            "event": "playback_drained",
            "turn_epoch": 4,
            "notification_id": None,
        })
        assert session._speaking is True

    asyncio.run(run())


def test_barge_in_classifier_failure_defers_without_character_fallback(monkeypatch):
    async def run():
        session, _ws = _make_session()

        async def unavailable(*_args, **_kwargs):
            return ""

        def forbidden_regex(*_args, **_kwargs):
            raise AssertionError("classifier outage used a character-level semantic fallback")

        session._quick_classify = unavailable
        monkeypatch.setattr("app.services.voice_classifiers.re.fullmatch", forbidden_regex)
        assert await session._classify_barge_in("嗯") == "defer"

    asyncio.run(run())


def test_retained_stale_answer_keeps_pre_staleness_prefix():
    class PrefixLLM:
        client = type("Client", (), {"base_url": "https://example.invalid/v1"})()

        def __init__(self):
            self.session = None

        async def stream_chat_structured(self, _messages, **_kwargs):
            yield {"type": "content", "data": "第一段。"}
            self.session._latest_input_seq = 2
            yield {"type": "content", "data": "第二段。"}

    class OneChunkTTS:
        async def stream_tts(self, _text, style_instruction=None, timeout=60.0):
            yield b"\x00\x00"

    async def run():
        nonlocal_session = None
        # Bind the fake's session without hiding the production transition.
        nonlocal_session, _ws = _make_session()
        session = nonlocal_session
        session.tts = OneChunkTTS()
        session._latest_input_seq = 1
        played = []

        async def capture_audio(data):
            played.append((session._current_seg_text, data))

        async def noop(*_args, **_kwargs):
            return None

        async def retain(*_args, **_kwargs):
            return '{"suppress": false}'

        session._send_bytes = capture_audio
        session._persist_message = noop
        session._set_state = noop
        session._quick_classify = retain
        session._extra_body = lambda _svc=None: {}
        fake_llm = PrefixLLM()
        fake_llm.session = session
        session._build_llm = lambda purpose="voice": (fake_llm, "fake")
        consumer = asyncio.create_task(session._tts_consumer())
        generation = asyncio.create_task(session._generate_and_speak_impl("问题", input_seq=1))
        await generation
        assert [text for text, _data in played] == ["第一段。", "第二段。"]
        session._closed = True
        session._interrupt.set()
        await session._tts_queue.put(None)
        await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(run())


def test_stale_approved_audio_can_play_without_text_filtering():
    class OneChunkTTS:
        async def stream_tts(self, _text, style_instruction=None, timeout=60.0):
            yield b"\x00\x00"

    async def run():
        session, _ws = _make_session()
        session.tts = OneChunkTTS()
        sent = []

        async def capture_audio(data):
            sent.append(data)

        session._send_bytes = capture_audio
        session._latest_input_seq = 2
        consumer = asyncio.create_task(session._tts_consumer())
        await session._tts_queue.put({
            "text": "经批准保留的实质回答",
            "epoch": 1,
            "input_seq": 1,
            "stale_approved": True,
        })
        await asyncio.sleep(0.05)
        assert sent
        session._closed = True
        session._interrupt.set()
        await session._tts_queue.put(None)
        await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(run())


def test_interjection_cancelled_during_event_emit_never_reenters_playback():
    async def run():
        session, _ws = _make_session()
        session._turn_epoch = 1

        async def cancel_during_interjection(event):
            if event.get("event") == "interjection":
                await session._request_interrupt()

        session._send_json = cancel_during_interjection
        await session._speak_interjection("会被取消的旧插话", "calm")
        assert session._turn_epoch == 1
        assert session._interrupt.is_set()
        assert session._tts_queue.empty()

    asyncio.run(run())


def test_resumed_notification_requires_new_audio_and_final_flush_with_epoch():
    async def run():
        session, _ws = _make_session()

        async def noop(_event):
            return None

        session._send_json = noop
        session._turn_epoch = 2
        session._playback_paused = True
        session._turn_reply_text = "通知的剩余内容"
        session._paused_spoken_chars = 0
        session._active_notification_id = "N1"
        session._current_notice_id = "N1"
        session._notification_states["N1"] = {
            "audio_sent": True,
            "generation_complete": True,
            "delivered": False,
            "burst_seq": 0,
            "llm_succeeded": True,
        }
        session._resume_epoch = 2
        session._resume_text = "通知的剩余内容"
        session._resume_q = asyncio.Queue()
        await session._resume_q.put(None)
        await session._resume_playback()
        state = session._notification_states["N1"]
        assert state["audio_sent"] is False
        assert state["generation_complete"] is False
        flushes = [item for item in list(session._tts_queue._queue) if isinstance(item, dict) and "flush" in item]
        assert any(item.get("epoch") == 2 and item.get("notification_complete") is True for item in flushes)

    asyncio.run(run())


def test_same_notification_old_epoch_ack_cannot_settle_retry():
    async def run():
        session, _ws = _make_session()

        async def noop(_event):
            return None

        session._send_json = noop
        session._turn_epoch = 2
        session._speaking_epoch = 2
        session._speaking = True
        session._active_notification_id = "N1"
        session._current_notice_id = "N1"
        session._notification_states["N1"] = {
            "audio_sent": True,
            "generation_complete": True,
            "delivered": False,
            "burst_seq": 0,
        }
        await session._handle_client_event({
            "event": "playback_drained",
            "notification_id": "N1",
            "turn_epoch": 1,
            "notice_burst_seq": 0,
        })
        assert session._notification_states["N1"]["delivered"] is False
        assert session._speaking is True

    asyncio.run(run())


def test_cancelled_direct_notice_releases_replay_latch_and_stays_incomplete():
    async def run():
        session, _ws = _make_session()
        session._queued_notice_ids.add("N1")

        async def cancel_during_notice(event):
            if event.get("event") == "bg_task_notice":
                await session._request_interrupt()

        session._send_json = cancel_during_notice
        await session._deliver_deterministic_notice({
            "notification_mode": "deterministic_v1",
            "task_status": "completed",
            "title": "测试通知",
        }, notification_id="N1")
        state = session._notification_states["N1"]
        assert state["generation_complete"] is False
        assert "N1" not in session._queued_notice_ids

    asyncio.run(run())


def test_original_notice_tail_ack_is_valid_after_turn_advances():
    async def run():
        session, _ws = _make_session()

        async def noop(_event):
            return None

        session._send_json = noop
        session._turn_epoch = 2
        session._speaking_epoch = 1
        session._speaking = True
        session._active_notification_id = "N1"
        session._current_notice_id = None
        session._notification_states["N1"] = {
            "audio_sent": True,
            "generation_complete": True,
            "delivered": False,
            "burst_seq": 0,
            "turn_epoch": 1,
        }
        await session._handle_client_event({
            "event": "playback_drained",
            "notification_id": "N1",
            "turn_epoch": 1,
            "notice_burst_seq": 0,
        })
        assert session._notification_states["N1"]["delivered"] is True

    asyncio.run(run())


def test_notice_batch_survives_fragment_coalescing():
    async def run():
        session, _ws = _make_session()
        batch = {"notice_batch": {"batch_id": "batch:1", "items": []}}
        await session._turn_queue.put(batch)
        merged = await session._coalesce_fragments("用户问题。", skip_wait=True, input_seq=1)
        assert merged == "用户问题。"
        assert session._turn_queue.get_nowait() is batch
        assert session._coalesced_input_seqs == [1]

    asyncio.run(run())


def test_pre_interrupt_direct_ack_cannot_clear_or_replace_the_new_epoch():
    async def run():
        session, _ws = _make_session()
        session._turn_epoch = 2
        session._interrupt.set()
        await session._speak_direct_text(
            "过期的确认", origin_epoch=1, turn_complete=False,
        )
        assert session._turn_epoch == 2
        assert session._interrupt.is_set()
        assert session._tts_queue.empty()

    asyncio.run(run())


def test_degraded_barge_in_text_cannot_override_structured_protocol():
    async def run():
        session, _ws = _make_session()

        async def degraded(*_args, **_kwargs):
            return "do not interrupt; defer to the current answer"

        session._quick_classify = degraded
        assert await session._classify_barge_in("用户插话") == "defer"

    asyncio.run(run())


def test_stale_output_suppression_uses_agentic_verdict_not_a_lexicon():
    async def run():
        session, _ws = _make_session()

        async def judge(*_args, **_kwargs):
            return '{"suppress": true}'

        session._quick_classify = judge
        session._generation_input_seq = 1
        session._latest_input_seq = 2
        assert await session._should_suppress_stale_reply("旧回答", user_turn=True) is True

    asyncio.run(run())


def test_delegation_guards_use_agentic_structured_verdicts():
    async def run():
        session, _ws = _make_session()
        calls = []

        async def judge(system, user, purpose="voice", response_format=None):
            calls.append((system, user, purpose, response_format))
            if "不要委派" in user:
                return '{"requested": false}'
            return '{"claimed": true}'

        session._quick_classify = judge
        assert await session._user_turn_requests_delegation("不要委派，直接回答") is False
        assert await session._claims_delegation_success("已交给研究助手执行") is True
        assert len(calls) == 2
        assert all(call[2] == "voice.delegation_guard" for call in calls)

    asyncio.run(run())


def test_runtime_turn_pipeline_has_no_regex_noise_shortcut():
    sources = [
        inspect.getsource(VoiceDuplexSession._on_user_turn_locked),
        inspect.getsource(VoiceDuplexSession._handle_user_turn),
        inspect.getsource(VoiceDuplexSession._asr_pipeline),
    ]
    assert all("_ASR_NOISE_RE" not in source for source in sources)
    assert "_is_voice_noise" not in inspect.getsource(VoiceDuplexSession._handle_user_turn)


def test_tts_segments_use_sentence_not_clause_boundaries():
    premature, pending = _split_segments("清晨有雾，")
    assert premature == [], "逗号到达时不得提前发起独立 TTS 请求"
    assert pending == "清晨有雾，"

    segments, rest = _split_segments("清晨有雾，阳光柔和，适合散步。")
    assert segments == ["清晨有雾，阳光柔和，适合散步。"]
    assert rest == ""

    tagged, tagged_rest = _split_segments("(平静)第一句，仍在同一句。第二句")
    assert tagged == ["(平静)第一句，仍在同一句。"]
    assert tagged_rest == "第二句"

    english, english_rest = _split_segments("First sentence.")
    assert english == ["First sentence."]
    assert english_rest == ""
    decimal, decimal_rest = _split_segments("The value is 3.14")
    assert decimal == []
    assert decimal_rest == "The value is 3.14"
    domain, domain_rest = _split_segments("Visit example.com")
    assert domain == []
    assert domain_rest == "Visit example.com"
    decimal_prefix, decimal_prefix_rest = _split_segments("The value is 3.")
    assert decimal_prefix == []
    assert decimal_prefix_rest == "The value is 3."
    quoted, quoted_rest = _split_segments('He said "Hello." Next')
    assert quoted == ['He said "Hello."']
    assert quoted_rest == " Next"


def test_notice_and_batch_never_count_as_pending_user_turns():
    async def run():
        session, _ws = _make_session()
        await session._turn_queue.put({"notice": {"task_id": "T1"}})
        await session._turn_queue.put({
            "notice_batch": {"batch_id": "batch:1", "items": []},
        })
        assert session._has_pending_user_turn() is False
        await session._turn_queue.put({"text": "真正的用户问题", "input_seq": 1})
        assert session._has_pending_user_turn() is True

    asyncio.run(run())


class _OneChunkTTS:
    async def stream_tts(self, _text, style_instruction=None, timeout=60.0):
        yield b"\x00\x00"


async def _assert_deterministic_resume(kind):
    session, _ws = _make_session()

    async def noop(*_args, **_kwargs):
        return None

    session._send_json = noop
    session._set_state = noop
    session.tts = _OneChunkTTS()
    consumer = asyncio.create_task(session._tts_consumer())
    try:
        if kind == "single":
            notification_id = "N1"
            await session._deliver_deterministic_notice({
                "notification_mode": "deterministic_v1",
                "task_status": "completed",
                "assistant_name": "执行助手",
                "title": "测试结果",
            }, notification_id=notification_id)
        else:
            notification_id = "batch:1"
            await session._deliver_notice_batch({
                "batch_id": notification_id,
                "items": [{
                    "notification_id": "N1",
                    "task_status": "completed",
                    "assistant_name": "执行助手",
                    "title": "第一项",
                }, {
                    "notification_id": "N2",
                    "task_status": "failed",
                    "assistant_name": "执行助手",
                    "title": "第二项",
                }],
            })
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

    full = "".join(segment["text"] for segment in session._turn_segments)
    assert full
    remaining = full[3:].strip()
    session._playback_paused = True
    session._paused_spoken_chars = 3
    session._resume_epoch = session._turn_epoch
    session._resume_text = remaining
    session._resume_q = asyncio.Queue()
    await session._resume_q.put(None)
    await session._resume_playback()

    items = list(session._tts_queue._queue)
    state = session._notification_states[notification_id]
    assert session._turn_reply_text == full
    assert state["llm_succeeded"] is True
    assert any(
        item.get("text") == remaining
        and item.get("notification_id") == notification_id
        for item in items if isinstance(item, dict)
    )
    assert any(
        item.get("notification_complete") is True
        and item.get("notification_id") == notification_id
        and item.get("epoch") == session._turn_epoch
        for item in items if isinstance(item, dict)
    )
    assert state["delivered"] is False, "resume must not ACK before new audio drains"


def test_deterministic_notice_pause_resume_restores_tail_and_completion():
    asyncio.run(_assert_deterministic_resume("single"))


def test_deterministic_batch_pause_resume_restores_tail_and_completion():
    asyncio.run(_assert_deterministic_resume("batch"))


def test_direct_speech_pause_resume_restores_final_flush():
    async def run():
        session, _ws = _make_session()

        async def noop(*_args, **_kwargs):
            return None

        session._send_json = noop
        session._speak_text = noop
        await session._speak_direct_text("直接插话的前半。后半。", wait=True)
        epoch = session._turn_epoch
        session._playback_paused = True
        session._turn_reply_text = "直接插话的前半。后半。"
        session._paused_spoken_chars = 8
        session._resume_epoch = epoch
        session._resume_text = "后半。"
        session._resume_q = asyncio.Queue()
        await session._resume_q.put(None)
        await session._resume_playback()

        assert session._final_flush_enqueued_epoch == epoch
        assert any(
            item.get("turn_complete") is True and item.get("epoch") == epoch
            for item in session._tts_queue._queue if isinstance(item, dict)
        )

    asyncio.run(run())


def test_resume_invalidates_previous_burst_completion_before_new_audio():
    async def run():
        session, _ws = _make_session()
        events = []

        async def capture(event):
            events.append(event)

        session._send_json = capture
        session._turn_epoch = 1
        session._speaking_epoch = 1
        session._speaking = True
        session._completed_speaking_epoch = 1
        session._final_flush_enqueued_epoch = 1
        session._playback_paused = True
        session._turn_reply_text = "已播前半。未播后半。"
        session._paused_spoken_chars = 5
        session._resume_epoch = 1
        session._resume_text = "未播后半。"
        session._resume_q = asyncio.Queue()
        await session._resume_q.put(None)
        await session._resume_playback()
        await session._handle_client_event({
            "event": "playback_drained",
            "turn_epoch": 1,
            "notification_id": None,
        })

        assert session._completed_speaking_epoch != 1
        assert session._speaking is True, "old final flush must not complete the resumed burst"
        assert any(event.get("event") == "notification_ack_pending" for event in events)

    asyncio.run(run())


async def _noop_event(_event):
    return None


def _probe_notice_settlement(monkeypatch, result):
    calls = []

    async def mark(_db, notification_id, delivery_attempt_id=None):
        calls.append((notification_id, delivery_attempt_id))
        outcome = result
        if isinstance(result, list):
            outcome = result[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    class DBContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr("app.db.database.AsyncSessionLocal", lambda: DBContext())
    monkeypatch.setattr(
        "app.services.voice_notification_service.mark_voice_notification_delivered",
        mark,
    )
    return calls


def _arm_zero_remaining(session, notification_id, **extra_state):
    session._send_json = _noop_event
    session._turn_epoch = 2
    session._playback_paused = True
    session._turn_reply_text = "通知已经全部播完。"
    session._paused_spoken_chars = len(session._turn_reply_text)
    session._active_notification_id = notification_id
    session._current_notice_id = None
    session._notification_states[notification_id] = {
        "audio_sent": True,
        "generation_complete": True,
        "llm_succeeded": True,
        "tts_failed": False,
        "delivered": False,
        **extra_state,
    }


def test_zero_remaining_batch_settles_member_ids_and_attempts(monkeypatch):
    async def run():
        session, _ws = _make_session()
        calls = _probe_notice_settlement(monkeypatch, True)
        session._queued_notice_ids.update({"N1", "N2"})
        _arm_zero_remaining(session, "batch:1", members=[{
            "notification_id": "N1", "delivery_attempt_id": "A1",
        }, {
            "notification_id": "N2", "delivery_attempt_id": "A2",
        }])
        await session._resume_playback()

        assert calls == [("N1", "A1"), ("N2", "A2")]
        assert session._notification_states["batch:1"]["delivered"] is True
        assert session._active_notification_id is None
        assert not session._queued_notice_ids.intersection({"N1", "N2"})

    asyncio.run(run())


def test_zero_remaining_single_settlement_preserves_attempt_provenance(monkeypatch):
    async def run():
        session, _ws = _make_session()
        calls = _probe_notice_settlement(monkeypatch, True)
        session._queued_notice_ids.add("N1")
        _arm_zero_remaining(session, "N1", delivery_attempt_id="A1")
        await session._resume_playback()

        assert calls == [("N1", "A1")]
        assert session._notification_states["N1"]["delivered"] is True
        assert "N1" not in session._queued_notice_ids

    asyncio.run(run())


def test_zero_remaining_failed_settlement_does_not_claim_delivery(monkeypatch):
    async def run():
        session, _ws = _make_session()
        calls = _probe_notice_settlement(monkeypatch, False)
        session._queued_notice_ids.add("N1")
        _arm_zero_remaining(session, "N1", delivery_attempt_id="A1")
        await session._resume_playback()

        assert calls == [("N1", "A1")]
        assert session._notification_states["N1"]["delivered"] is False
        assert session._active_notification_id is None
        assert "N1" not in session._queued_notice_ids, "failed durable ACK must remain replayable"

    asyncio.run(run())


def test_zero_remaining_without_audio_evidence_cannot_settle(monkeypatch):
    async def run():
        session, _ws = _make_session()
        calls = _probe_notice_settlement(monkeypatch, True)
        session._queued_notice_ids.add("N1")
        _arm_zero_remaining(session, "N1", audio_sent=False, delivery_attempt_id="A1")
        await session._resume_playback()

        assert calls == []
        assert session._notification_states["N1"]["delivered"] is False
        assert session._active_notification_id is None
        assert "N1" not in session._queued_notice_ids

    asyncio.run(run())


def test_notice_preparation_preserves_current_audible_answer_text():
    async def run():
        session, _ws = _make_session()
        session._turn_reply_text = "仍在播放的旧回答。"
        session._speaking = True
        seen = []

        async def noop(*_args, **_kwargs):
            return None

        async def wait_for_old_tail(*_args, **_kwargs):
            seen.append(session._turn_reply_text)
            return True

        session._send_json = noop
        session._set_state = noop
        session._speak_direct_text = wait_for_old_tail
        await session._deliver_deterministic_notice({
            "notification_mode": "deterministic_v1",
            "task_status": "completed",
            "title": "后续通知",
        }, notification_id="N1")

        assert seen == ["仍在播放的旧回答。"]
        assert session._turn_reply_text == "仍在播放的旧回答。"

    asyncio.run(run())


def test_direct_pcm_owns_its_text_after_a_deterministic_notice():
    async def run():
        session, _ws = _make_session()
        session.tts = _OneChunkTTS()
        session._turn_reply_text = "上一条确定性通知。"
        await session._speak_direct_text("新的普通直接插话。", wait=False)
        assert session._turn_reply_text == "上一条确定性通知。", "queued PCM must not preempt audible text"

        consumer = asyncio.create_task(session._tts_consumer())
        try:
            await asyncio.sleep(0.05)
            assert session._turn_reply_text == "新的普通直接插话。"
        finally:
            session._closed = True
            session._interrupt.set()
            await session._tts_queue.put(None)
            await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(run())


def test_pause_before_first_notice_pcm_keeps_old_answer_ownership():
    class BlockingTTS:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.resume_failed = asyncio.Event()
            self.calls = 0

        async def stream_tts(self, _text, style_instruction=None, timeout=60.0):
            self.calls += 1
            if self.calls > 1:
                self.resume_failed.set()
                raise RuntimeError("old answer resume synthesis failed")
            self.entered.set()
            await self.release.wait()
            yield b"\x00\x00"

    async def run():
        session, _ws = _make_session()
        tts = BlockingTTS()
        session.tts = tts
        session._turn_epoch = 1
        session._speaking_epoch = 1
        session._speaking = True
        session._turn_reply_text = "旧回答的前半。旧回答的后半。"
        session._final_flush_enqueued_epoch = 1
        session._queued_notice_ids.add("N1")
        events = []

        async def capture(event):
            events.append(event)

        async def noop(*_args, **_kwargs):
            return None

        session._send_json = capture
        session._set_state = noop
        allow_delivery_return = asyncio.Event()
        original_speak_direct = session._speak_direct_text

        async def gated_direct(*args, **kwargs):
            played = await original_speak_direct(*args, **kwargs)
            await allow_delivery_return.wait()
            return played

        session._speak_direct_text = gated_direct
        consumer = asyncio.create_task(session._tts_consumer())
        delivery = asyncio.create_task(session._deliver_deterministic_notice({
            "notification_mode": "deterministic_v1",
            "task_status": "completed",
            "title": "尚未发声的通知",
        }, notification_id="N1"))
        try:
            await asyncio.wait_for(tts.entered.wait(), timeout=1.0)
            await session._pause_playback()
            await asyncio.wait_for(tts.resume_failed.wait(), timeout=1.0)
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

            assert session._turn_reply_text == "旧回答的前半。旧回答的后半。"
            assert session._active_notification_id is None
            assert session._current_notice_id == "N1", "delivery must still be in its preparation scope"
            expected_resume = session._resume_text
            assert expected_resume
            await session._resume_playback()
            items = list(session._tts_queue._queue)
            assert any(
                item.get("text") == expected_resume and item.get("notification_id") is None
                for item in items if isinstance(item, dict)
            )
            assert not any(item.get("notification_complete") for item in items if isinstance(item, dict))
            resumed = next(event for event in events if event.get("event") == "playback_resumed")
            assert resumed.get("notification_id") is None
        finally:
            tts.release.set()
            allow_delivery_return.set()
            await asyncio.gather(delivery, return_exceptions=True)
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

        state = session._notification_states["N1"]
        assert state["audio_sent"] is False
        assert state["generation_complete"] is False
        assert state.get("tts_failed") is not True
        assert not any(event.get("event") == "notification_failed" for event in events)
        assert "N1" not in session._queued_notice_ids

    asyncio.run(run())


def test_pre_pcm_batch_current_id_does_not_own_immediate_resume():
    async def run():
        session, _ws = _make_session()
        session._send_json = _noop_event
        session._turn_epoch = 1
        session._final_flush_enqueued_epoch = 1
        session._playback_paused = True
        session._turn_reply_text = "旧回答前半。旧回答后半。"
        session._paused_spoken_chars = 5
        session._current_notice_id = "batch:1"
        session._active_notification_id = None
        session._notification_states["batch:1"] = {
            "audio_sent": False,
            "generation_complete": False,
            "llm_succeeded": True,
            "tts_failed": False,
            "delivered": False,
        }
        session._resume_epoch = 1
        session._resume_text = "旧回答后半。"
        session._resume_q = asyncio.Queue()
        await session._resume_q.put(None)
        await session._resume_playback()

        items = list(session._tts_queue._queue)
        assert any(
            item.get("text") == "旧回答后半。" and item.get("notification_id") is None
            for item in items if isinstance(item, dict)
        )
        assert not any(item.get("notification_complete") for item in items if isinstance(item, dict))

    asyncio.run(run())


def test_resume_delta_does_not_inherit_preparing_notice():
    async def run():
        session, _ws = _make_session()
        session._send_json = _noop_event
        session._turn_epoch = 1
        session._final_flush_enqueued_epoch = 1
        session._playback_paused = True
        session._turn_reply_text = "旧回答前半。旧回答后半。追加内容。"
        session._paused_spoken_chars = 5
        session._current_notice_id = "N1"
        session._active_notification_id = None
        session._resume_epoch = 1
        session._resume_text = "旧回答后半。"
        session._resume_q = asyncio.Queue()
        await session._resume_q.put(None)
        await session._resume_playback()

        texts = [item for item in session._tts_queue._queue if isinstance(item, dict) and item.get("text")]
        assert [item.get("text") for item in texts] == ["旧回答后半。", "追加内容。"]
        assert all(item.get("notification_id") is None for item in texts)

    asyncio.run(run())


def test_fresh_resume_without_snapshot_does_not_inherit_preparing_notice():
    async def run():
        session, _ws = _make_session()
        session._send_json = _noop_event
        session._turn_epoch = 1
        session._final_flush_enqueued_epoch = 1
        session._playback_paused = True
        session._turn_reply_text = "旧回答前半。旧回答后半。"
        session._paused_spoken_chars = 5
        session._current_notice_id = "N1"
        session._active_notification_id = None
        real_speak_text = session._speak_text

        async def without_wait(*args, **kwargs):
            kwargs["wait"] = False
            return await real_speak_text(*args, **kwargs)

        session._speak_text = without_wait
        await session._resume_playback()
        item = next(item for item in session._tts_queue._queue if isinstance(item, dict) and item.get("text"))
        assert item.get("text") == "旧回答后半。"
        assert item.get("notification_id") is None

    asyncio.run(run())


def test_ordinary_direct_speech_does_not_inherit_preparing_notice():
    async def run():
        session, _ws = _make_session()
        session._current_notice_id = "N1"
        session._active_notification_id = None
        await session._speak_direct_text("普通直接插话。", wait=False)
        item = next(item for item in session._tts_queue._queue if isinstance(item, dict) and item.get("text"))
        assert item.get("notification_id") is None

    asyncio.run(run())


def test_empty_direct_pcm_never_claims_reply_text():
    class EmptyTTS:
        async def stream_tts(self, _text, style_instruction=None, timeout=60.0):
            yield b""

    async def run():
        session, _ws = _make_session()
        session.tts = EmptyTTS()
        session._turn_reply_text = "仍可恢复的旧回答。"
        consumer = asyncio.create_task(session._tts_consumer())
        try:
            await session._speak_direct_text("没有音频的新插话。", wait=True)
            assert session._turn_reply_text == "仍可恢复的旧回答。"
        finally:
            session._closed = True
            session._interrupt.set()
            await session._tts_queue.put(None)
            await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(run())


def test_zero_remaining_batch_partial_failure_and_exception_are_retryable(monkeypatch):
    async def run():
        for outcomes in ([True, False], [True, RuntimeError("commit failed")]):
            session, _ws = _make_session()
            calls = _probe_notice_settlement(monkeypatch, outcomes)
            session._queued_notice_ids.update({"N1", "N2"})
            _arm_zero_remaining(session, "batch:1", members=[{
                "notification_id": "N1", "delivery_attempt_id": "A1",
            }, {
                "notification_id": "N2", "delivery_attempt_id": "A2",
            }])
            await session._resume_playback()

            assert calls == [("N1", "A1"), ("N2", "A2")]
            assert session._notification_states["batch:1"]["delivered"] is False
            assert session._active_notification_id is None
            assert not session._queued_notice_ids.intersection({"N1", "N2"})

    asyncio.run(run())


def test_durable_ack_helper_rejects_stale_attempt_without_commit():
    async def run():
        from app.services.voice_notification_service import mark_voice_notification_delivered

        class Row:
            status = "pending"
            delivery_attempt_id = "new-attempt"
            delivered_at = None
            delivery_lease_owner = "voice-session"
            delivery_lease_expires_at = object()

        class DB:
            def __init__(self):
                self.row = Row()
                self.commits = 0

            async def get(self, _model, notification_id):
                assert notification_id == "N1"
                return self.row

            async def commit(self):
                self.commits += 1

        db = DB()
        assert await mark_voice_notification_delivered(db, "N1", "old-attempt") is False
        assert db.row.status == "pending"
        assert db.commits == 0

    asyncio.run(run())
