"""A preempted synchronous tool turn must not end with only 'I will do it'."""
import asyncio
import json

from app.services.voice_service import VoiceDuplexSession
from app.tools.registry import registry


class _ScriptedLLM:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = []

    async def stream_chat_structured(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if not self.rounds:
            return
        current = self.rounds.pop(0)
        if isinstance(current, Exception):
            raise current
        for event in current:
            yield event


def _tool_round():
    call = [{
        "id": "call-1",
        "type": "function",
        "function": {"name": "notes", "arguments": json.dumps({"action": "list_notebooks"})},
    }]
    return [
        {"type": "content", "data": "好的，我先做。"},
        {"type": "tool_calls", "data": call},
    ]


def _make_session(llm, monkeypatch):
    s = VoiceDuplexSession(None, None, None, conversation_id=None)
    s._workspace_path = ""
    s._set_state = _noop
    s._speak_text = _noop
    s._send_json = _noop
    s._persist_turn_progress = _noop
    s._persist_message = _noop
    s._schedule_filler_ttft_watch = lambda: None
    s._clear_pending_filler = lambda: None
    s._extra_body = lambda svc=None: {}
    s._build_llm = lambda purpose="voice": (llm, "fake")
    s._test_events = []
    s._test_persisted = []
    s._test_park_progress = []

    async def _send_json(ev):
        s._test_events.append(ev)

    async def _persist(role, content, **kwargs):
        s._test_persisted.append((role, content))

    s._send_json = _send_json
    s._persist_message = _persist

    async def _park_progress():
        s._test_park_progress.append(s._turn_msg_id)

    s._park_preempted_tool_preamble = _park_progress

    async def _dispatch(name, args, **kwargs):
        return json.dumps({"success": True, "notebooks": [{"name": "工作"}]}, ensure_ascii=False)

    monkeypatch.setattr(registry, "dispatch", _dispatch)
    monkeypatch.setattr(
        registry,
        "get_definitions",
        lambda names, quiet=True: [{"type": "function", "function": {"name": "notes", "parameters": {}}}],
    )
    return s


def test_summary_pending_survives_partial_content_then_provider_error(monkeypatch):
    async def run():
        llm = _ScriptedLLM([
            _tool_round(),
            [{"type": "content", "data": "我先"}, {"type": "error", "data": "provider down after partial content"}],
        ])
        s = _make_session(llm, monkeypatch)
        drain = asyncio.create_task(_drain_tts(s))
        try:
            await s._turn_queue.put({"text": "新问题", "input_seq": 2})
            await s._generate_and_speak_impl("先查一下", allow_tools=True, allow_filler=False)
            s._interrupt.clear()  # responder 的下一有效回合入口语义
            await s._generate_and_speak_impl("新问题", allow_tools=True, allow_filler=False)
        finally:
            drain.cancel()
        assert getattr(s, "_pending_tool_summary_note", ""), "部分正文后出错仍不得消费欠总结"

    asyncio.run(run())


def test_notice_only_queue_does_not_cancel_tool_turn(monkeypatch):
    async def run():
        llm = _ScriptedLLM([_tool_round(), [{"type": "content", "data": "工具结果已经查清。"}]])
        s = _make_session(llm, monkeypatch)
        drain = asyncio.create_task(_drain_tts(s))
        try:
            await s._turn_queue.put({"notice": {"task_id": "background-N"}, "notification_id": "background-N"})
            await s._generate_and_speak_impl("先查一下", allow_tools=True, allow_filler=False)
        finally:
            drain.cancel()
        assert len(llm.calls) == 2, "只有通知排队时必须继续生成工具结果总结"
        assert not any(ev.get("event") == "generation_cancelled" for ev in s._test_events), s._test_events

    asyncio.run(run())


async def _noop(*args, **kwargs):
    return None


async def _drain_tts(s):
    while True:
        item = await s._tts_queue.get()
        if item is None:
            return
        if isinstance(item, dict) and "flush" in item:
            fut = item["flush"]
            if fut and not fut.done():
                fut.set_result(True)


def test_preempted_tool_result_produces_and_consumes_summary_pending(monkeypatch):
    async def run():
        llm = _ScriptedLLM([_tool_round(), [{"type": "content", "data": "查好了，也回答你的新问题。"}]])
        s = _make_session(llm, monkeypatch)
        drain = asyncio.create_task(_drain_tts(s))
        try:
            await s._turn_queue.put({"text": "新问题", "input_seq": 2})
            await s._generate_and_speak_impl("先查一下", allow_tools=True, allow_filler=False)
            note = getattr(s, "_pending_tool_summary_note", "")
            assert note, "被抢占的工具结果必须留下 summary_pending"
            assert not any(role == "assistant" and "我先做" in content for role, content in s._test_persisted), s._test_persisted
            assert not any(ev.get("event") == "assistant_text" and ev.get("done") and "我先做" in (ev.get("text") or "") for ev in s._test_events), s._test_events
            assert s._test_park_progress, "被抢占的前奏必须转为工具记录，真实 tool_calls/results 不得删除"

            s._interrupt.clear()  # responder 的下一有效回合入口语义
            await s._generate_and_speak_impl("新问题", allow_tools=True, allow_filler=False)
        finally:
            drain.cancel()

        followup = llm.calls[1]["messages"]
        system_text = "\n".join(m.get("content") or "" for m in followup if m.get("role") == "system")
        assert "工具" in system_text and "结果" in system_text
        assert "尚未" in system_text or "没有来得及" in system_text
        assert not getattr(s, "_pending_tool_summary_note", ""), "成功交代后必须消费提示，避免每轮重复"

    asyncio.run(run())


def test_summary_pending_survives_failed_followup_generation(monkeypatch):
    async def run():
        llm = _ScriptedLLM([_tool_round(), RuntimeError("provider down")])
        s = _make_session(llm, monkeypatch)
        drain = asyncio.create_task(_drain_tts(s))
        try:
            await s._turn_queue.put({"text": "新问题", "input_seq": 2})
            await s._generate_and_speak_impl("先查一下", allow_tools=True, allow_filler=False)
            assert getattr(s, "_pending_tool_summary_note", "")
            s._interrupt.clear()  # responder 的下一有效回合入口语义
            await s._generate_and_speak_impl("新问题", allow_tools=True, allow_filler=False)
        finally:
            drain.cancel()

        assert getattr(s, "_pending_tool_summary_note", ""), "生成失败不得静默丢失欠总结"

    asyncio.run(run())
