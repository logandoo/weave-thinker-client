"""Service-level integration — voice truncated tool-call dispatch path.

Exercises the REAL `VoiceDuplexSession._generate_and_speak_impl` tool loop with
a scripted LLM stream: round 1 replays the production truncated `create_note`
arguments payload (conv fcb2e60a, max_tokens budget cut the JSON mid-string),
later rounds replay the recovery. Verifies:
  * the truncated call is NOT dispatched (old bug: silent `{}` → "Unknown action"),
  * the model receives an actionable truncation tool result,
  * the per-turn output budget doubles (bounded) for the retry,
  * the payload re-sent to the provider is valid JSON (`{}` normalization),
  * a subsequent valid (chunked) call IS dispatched and the reply is persisted.

Run from backend/:  ../.venv/bin/python -m pytest tests/test_voice_truncation_dispatch_integration.py -v
"""
import asyncio
import json
from pathlib import Path

from app.core.config import get_config
from app.services.voice_service import VoiceDuplexSession
from app.tools.registry import registry

FIXTURE = Path(__file__).parent / "fixtures" / "voice_truncated_create_note_args.txt"

VALID_CHUNKED_CALL = json.dumps(
    {"action": "create_note", "notebook_id": "nb-1", "title": "医学教育数据中台（1/3）",
     "content": "## 一、总体思路\n\n自建数据中台；现阶段靠导入。"},
    ensure_ascii=False,
)


class _DummyWS:
    def __init__(self):
        self.sent = []
        self.json_events = []

    async def send_bytes(self, data):
        self.sent.append(data)

    async def send_text(self, text):
        self.json_events.append(text)


class _ScriptedLLM:
    """Yields predefined event lists per round; records each call's budget and
    a snapshot of the messages it received."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = []

    async def stream_chat_structured(self, messages, **kwargs):
        self.calls.append({
            "max_tokens": kwargs.get("max_tokens"),
            "messages": [dict(m) for m in messages],
        })
        events = self.rounds.pop(0) if self.rounds else [{"type": "content", "data": "结束"}]
        for ev in events:
            yield ev


def _tool_call(call_id, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "notes", "arguments": arguments},
    }


async def _noop(*_a, **_k):
    return None


async def _drain_tts(queue):
    while True:
        item = await queue.get()
        fut = item.get("flush") if isinstance(item, dict) else None
        if fut is not None and not fut.done():
            fut.set_result(None)


def _make_session(llm, monkeypatch, dispatch_calls, persist_calls=None):
    s = VoiceDuplexSession(_DummyWS(), None, None, conversation_id=None)
    s._workspace_path = ""
    s._generation_messages = lambda extra_user=None: [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "把我们刚刚讨论的内容放到一个全新的笔记里"},
    ]
    s._set_state = _noop
    s._speak_text = _noop
    s._send_json = _noop
    if persist_calls is None:
        s._persist_turn_progress = _noop
    else:
        async def _persist(content, tool_calls=None, tool_results=None, final=False):
            persist_calls.append(tool_calls)
        s._persist_turn_progress = _persist
    s._persist_message = _noop
    s._schedule_filler_ttft_watch = lambda: None
    s._clear_pending_filler = lambda: None
    s._extra_body = lambda svc=None: {}
    s._build_llm = lambda purpose="voice": (llm, "fake-model")

    async def _dispatch(name, args, **_kw):
        dispatch_calls.append({"name": name, "args": args})
        return json.dumps({"success": True, "message": "已创建笔记"}, ensure_ascii=False)

    monkeypatch.setattr(registry, "dispatch", _dispatch)
    monkeypatch.setattr(
        registry, "get_definitions",
        lambda names, quiet=True: [
            {"type": "function", "function": {"name": "notes", "parameters": {}}},
        ],
    )
    return s


async def _run_turn(s, text="把我们刚刚讨论的内容放到一个全新的笔记里"):
    drain = asyncio.create_task(_drain_tts(s._tts_queue))
    try:
        await s._generate_and_speak_impl(text, allow_tools=True, allow_filler=False)
    finally:
        drain.cancel()


def _round(content, tool_calls=None, finish="stop"):
    events = [{"type": "content", "data": content}]
    if tool_calls is not None:
        events.append({"type": "tool_calls", "data": tool_calls})
    events.append({"type": "done", "data": {"finish_reason": finish}})
    return events


def test_truncated_call_not_dispatched_then_chunked_retry_succeeds(monkeypatch):
    trunc = FIXTURE.read_text(encoding="utf-8")
    monkeypatch.setitem(get_config().voice, "max_tokens", 1024)
    monkeypatch.setitem(get_config().voice, "max_tokens_ceiling", 8192)
    monkeypatch.setitem(get_config().voice, "truncation_retry_limit", 2)

    llm = _ScriptedLLM([
        _round("好，我来写入。", [_tool_call("call_trunc", trunc)], finish="length"),
        _round("内容较长，我改为分块写入。", [_tool_call("call_ok", VALID_CHUNKED_CALL)], finish="tool_calls"),
        _round("已创建笔记《医学教育数据中台（1/3）》。"),
    ])
    dispatch_calls = []
    persist_calls = []
    s = _make_session(llm, monkeypatch, dispatch_calls, persist_calls=persist_calls)

    asyncio.run(_run_turn(s))

    # budgets: 1024 (configured) -> 2048 after truncation -> stays 2048
    assert [c["max_tokens"] for c in llm.calls] == [1024, 2048, 2048]

    # round 1: truncated args never dispatched
    assert dispatch_calls == [] or dispatch_calls[0]["args"].get("title") != "医学教育数据中台 · 产品需求梳理"
    # recovery round: the chunked valid call is dispatched exactly once
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["name"] == "notes"
    assert dispatch_calls[0]["args"]["action"] == "create_note"
    assert dispatch_calls[0]["args"]["_permission_granted"] is True

    # messages re-sent to the provider in round 2: assistant call normalized to
    # parseable JSON + actionable truncation tool result carrying the call id
    round2 = llm.calls[1]["messages"]
    assistant_calls = [m for m in round2 if m.get("role") == "assistant" and m.get("tool_calls")]
    assert assistant_calls, "assistant tool_calls message missing in round 2"
    sent_args = [tc["function"]["arguments"] for tc in assistant_calls[-1]["tool_calls"]]
    assert sent_args and all(isinstance(a, str) for a in sent_args)
    json.loads(sent_args[0])  # must be valid JSON (was truncated before)
    tool_msgs = [m for m in round2 if m.get("role") == "tool"]
    tr_payloads = []
    for m in tool_msgs:
        try:
            data = json.loads(m["content"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("_truncated"):
            tr_payloads.append(data)
    assert tr_payloads, "actionable truncation tool result not fed back to the model"
    assert "截断" in tr_payloads[0]["error"] and "分块" in tr_payloads[0]["error"]

    # final assistant reply persisted to history
    assert s._history[-1]["role"] == "assistant"
    assert "已创建笔记" in s._history[-1]["content"]

    # forensic persistence: the ORIGINAL truncated payload stays in the saved
    # tool_calls record (acceptance §1 — 原样落档), while replay is normalized.
    persisted_args = []
    for tc_json in persist_calls:
        if not isinstance(tc_json, str):
            continue
        try:
            for t in json.loads(tc_json):
                persisted_args.append(t.get("function", {}).get("arguments", ""))
        except (json.JSONDecodeError, TypeError):
            continue
    assert trunc in persisted_args


class _FakeMessage:
    def __init__(self, role, content, tool_calls=None, tool_results=None):
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_results = tool_results
        self.created_at = 0


class _FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return iter(self._items)


class _FakeDB:
    def __init__(self, items):
        self._items = items

    async def execute(self, *_a, **_k):
        return _FakeResult(self._items)


def test_load_history_normalizes_truncated_arguments_for_replay(monkeypatch):
    """A4.9 r1 Important: reconnect must not replay unclosed JSON upstream."""
    trunc = FIXTURE.read_text(encoding="utf-8")
    tool_calls = json.dumps(
        [_tool_call("call_trunc", trunc)], ensure_ascii=False)
    tool_results = json.dumps(
        {"agent_steps": [{
            "name": "call_trunc",
            "title": "笔记操作",
            "content": json.dumps(
                {"error": "工具 notes 的参数被截断…", "_truncated": True},
                ensure_ascii=False),
        }]},
        ensure_ascii=False)
    msg = _FakeMessage(
        "assistant", "试了几次都报同一个错，创建笔记这一步没成功。",
        tool_calls=tool_calls, tool_results=tool_results,
    )
    s = VoiceDuplexSession(_DummyWS(), None, _FakeDB([msg]), conversation_id="conv-1")

    asyncio.run(s._load_history())

    with_calls = [m for m in s._history if m.get("tool_calls")]
    assert with_calls, "assistant tool_calls message not rehydrated"
    replayed = with_calls[0]["tool_calls"][0]["function"]["arguments"]
    json.loads(replayed)  # must be valid JSON for the provider
    assert replayed == "{}"
    tool_msgs = [m for m in s._history if m.get("role") == "tool"]
    assert tool_msgs and "_truncated" in tool_msgs[0]["content"]


def test_repeated_truncation_bounded_and_never_dispatched(monkeypatch):
    trunc = FIXTURE.read_text(encoding="utf-8")
    monkeypatch.setitem(get_config().voice, "max_tokens", 1024)
    monkeypatch.setitem(get_config().voice, "max_tokens_ceiling", 8192)
    monkeypatch.setitem(get_config().voice, "truncation_retry_limit", 2)

    llm = _ScriptedLLM([
        _round("好，我来写入。", [_tool_call("call_t1", trunc)], finish="length"),
        _round("我再试一次。", [_tool_call("call_t2", trunc)], finish="length"),
        _round("还是太长。", [_tool_call("call_t3", trunc)], finish="length"),
        _round("内容太长，请允许我分段写入；先不执行。"),
    ])
    dispatch_calls = []
    s = _make_session(llm, monkeypatch, dispatch_calls)

    asyncio.run(_run_turn(s))

    # two escalations then bounded: 1024 -> 2048 -> 4096 -> 4096
    assert [c["max_tokens"] for c in llm.calls] == [1024, 2048, 4096, 4096]
    # never dispatched with empty/partial args
    assert dispatch_calls == []
    assert s._history[-1]["role"] == "assistant"
    assert "太长" in s._history[-1]["content"] or "先不执行" in s._history[-1]["content"]
