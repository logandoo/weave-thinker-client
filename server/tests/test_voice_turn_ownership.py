"""Voice turn ownership: a newer accepted user turn supersedes stale low-value replies."""
import asyncio

from app.services.voice_service import VoiceDuplexSession


class _DummyWS:
    def __init__(self):
        self.sent = []
        self.json_events = []


class _FakeLLM:
    def __init__(self, text):
        self.text = text

    async def stream_chat_structured(self, messages, **kwargs):
        yield {"type": "content", "data": self.text}


def _make_session(text):
    s = VoiceDuplexSession(_DummyWS(), None, None, conversation_id=None)
    events = []
    persisted = []
    tts_items = []

    async def _send_json(ev):
        events.append(ev)

    async def _persist(role, content, **kwargs):
        persisted.append((role, content))

    async def _speak(text, wait=True, aux=False):
        return None

    s._send_json = _send_json
    s._persist_message = _persist
    s._speak_text = _speak
    s._set_state = _noop
    s._schedule_filler_ttft_watch = lambda: None
    s._clear_pending_filler = lambda: None

    async def no_claim(_text):
        return False

    async def deliver_stale(*_args, **_kwargs):
        return '{"suppress": false}'

    s._claims_delegation_success = no_claim
    s._quick_classify = deliver_stale
    s._extra_body = lambda svc=None: {}
    s._build_llm = lambda purpose="voice": (_FakeLLM(text), "fake")
    return s, events, persisted, tts_items


async def _noop(*args, **kwargs):
    return None


async def _drain_tts(s, items):
    while True:
        item = await s._tts_queue.get()
        if item is None:
            return
        if isinstance(item, dict) and item.get("text"):
            items.append(item)
        if isinstance(item, dict) and "flush" in item:
            fut = item["flush"]
            if fut and not fut.done():
                fut.set_result(True)


def test_enqueue_assigns_monotonic_input_seq_and_dedupes():
    s, _, _, _ = _make_session("回答")
    s._enqueue_turn("旧问题")
    s._enqueue_turn("旧问题")
    assert s._latest_input_seq == 1, "仍在队列中的重复轮不得推进 input_seq"
    first = s._turn_queue.get_nowait()
    assert first["input_seq"] == 1
    s._enqueue_turn("新问题")
    second = s._turn_queue.get_nowait()
    assert second["input_seq"] == 2


def test_stale_low_value_reply_is_not_finalized_or_spoken():
    async def run():
        s, events, persisted, tts_items = _make_session("(平静)你说，我听着。")

        async def suppress_stale(*_args, **_kwargs):
            return '{"suppress": true}'

        s._quick_classify = suppress_stale
        s._generation_input_seq = 1
        s._latest_input_seq = 2
        drain = asyncio.create_task(_drain_tts(s, tts_items))
        try:
            await s._generate_and_speak_impl("旧问题", allow_filler=False)
        finally:
            drain.cancel()

        assert not any(role == "assistant" for role, _ in persisted), persisted
        assert not tts_items, "过期低价值回执不得进入 TTS/PCM"
        assert not any(
            ev.get("event") == "assistant_text" and ev.get("done") for ev in events
        ), events
        assert any(ev.get("event") == "generation_cancelled" for ev in events), events
        assert s._interrupt.is_set(), "过期活动音频也必须停止"

    asyncio.run(run())


def test_stale_prefetched_low_value_reply_is_also_suppressed():
    async def run():
        s, events, persisted, _ = _make_session("(平静)好的。")

        async def suppress_stale(*_args, **_kwargs):
            return '{"suppress": true}'

        s._quick_classify = suppress_stale
        s._generation_input_seq = 1
        s._latest_input_seq = 2
        await s._generate_and_speak_impl("旧问题", prefetched="好的。", allow_filler=False)
        assert not any(role == "assistant" for role, _ in persisted), persisted
        assert not any(ev.get("event") == "assistant_text" and ev.get("done") for ev in events), events
        assert any(ev.get("event") == "generation_cancelled" for ev in events), events

    asyncio.run(run())


def test_stale_short_substantive_reply_is_not_misclassified_as_low_value():
    async def run():
        s, events, persisted, tts_items = _make_session("答案是2。")
        s._generation_input_seq = 1
        s._latest_input_seq = 2
        drain = asyncio.create_task(_drain_tts(s, tts_items))
        try:
            await s._generate_and_speak_impl("旧问题", allow_filler=False)
        finally:
            drain.cancel()
        assert any(role == "assistant" for role, _ in persisted), persisted
        assert tts_items, "短实质回答不能被长度阈值误杀"
        assert any(ev.get("event") == "assistant_text" and ev.get("done") for ev in events), events

    asyncio.run(run())


def test_fragment_coalescing_uses_latest_consumed_input_seq():
    async def run():
        s, _, _, _ = _make_session("回答")
        s._turn_queue.put_nowait({"text": "知道答案", "input_seq": 2})
        merged = await s._coalesce_fragments("我想", input_seq=1)
        assert merged == "我想知道答案"
        assert s._coalesced_input_seq == 2

    asyncio.run(run())


def test_coalesced_backchannel_rolls_back_all_ignored_fragments():
    async def run():
        s, _, _, _ = _make_session("回答")
        s._latest_input_seq = 3
        s._coalesced_input_seq = 3
        s._coalesced_input_seqs = [2, 3]
        s._queued_turn_is_backchannel = _async_true
        s._set_state = _noop
        await s._turn_queue.put({"text": "嗯，", "input_seq": 2, "semantic_complete": True})
        await s._turn_queue.put({"text": "你继续", "input_seq": 3})
        responder = asyncio.create_task(s._responder())
        try:
            await asyncio.sleep(0.05)
        finally:
            responder.cancel()
            await asyncio.gather(responder, return_exceptions=True)
        assert s._latest_input_seq == 1

    asyncio.run(run())


async def _async_true(*args, **kwargs):
    return True


def test_stale_substantive_reply_keeps_existing_defer_behavior():
    async def run():
        text = "(平静)这是一段包含实际分析和明确结论的回答，不是单纯的接话回执。"
        s, events, persisted, tts_items = _make_session(text)
        s._generation_input_seq = 1
        s._latest_input_seq = 2
        drain = asyncio.create_task(_drain_tts(s, tts_items))
        try:
            await s._generate_and_speak_impl("旧问题", allow_filler=False)
        finally:
            drain.cancel()

        assert any(role == "assistant" for role, _ in persisted), persisted
        assert tts_items, "实质回答仍按现有 defer 语义交付"
        assert any(
            ev.get("event") == "assistant_text" and ev.get("done") for ev in events
        ), events

    asyncio.run(run())
