"""W26 echo 通道回归（HANDOFF_upstream_w26 §B1/B2/B3）。

三项上游缺陷：
  B1 registry broadcast 溢出无差别丢队首 —— 契约事件（interjection_committed/
     done/error）必须优先保留，先淘汰最旧 delta backlog。
  B2 resume replay 载荷不含插话 echo —— 断连期 commit 的插话永久丢失 →
     前端 pending 永不确认 → 终态冲刷重发 → 双气泡。Buffer 必须记录并在
     to_replay()/to_snapshot() 暴露。
  B3 插话 echo 无 client_message_id —— 同文两条插话错配；请求体须携带并由
     echo 回带（drain 兼容旧 str 队列项与 agent_worker 的字符串投递）。

Run: cd backend && python -m pytest tests/test_interjection_echo_replay.py -q
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.active_agent_registry import ActiveAgentState  # noqa: E402
from app.services.agent_loop import AgentLoop, AgentLoopState  # noqa: E402
from app.services.stream_buffer import ConversationBuffer, StreamBufferManager  # noqa: E402


def _drain(q: asyncio.Queue):
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return items


def _full_state(events):
    st = ActiveAgentState(conversation_id="conv-w26")
    q: asyncio.Queue = asyncio.Queue(maxsize=len(events))
    for e in events:
        q.put_nowait(e)
    st._subscribers = [q]
    return st, q


# ── B1 契约事件不可丢 ──────────────────────────────────────────────────────

def test_broadcast_overflow_evicts_oldest_delta_not_contract_event():
    """队列满时广播 delta：最旧 delta 被淘汰，契约事件必须存活。"""
    st, q = _full_state([
        {"interjection_committed": {"content": "已落库插话", "id": "m1"}},
        {"content": "a"},
        {"content": "b"},
        {"content": "c"},
    ])

    asyncio.run(st.broadcast({"content": "new"}))

    items = _drain(q)
    assert any("interjection_committed" in e for e in items), items
    assert {"content": "a"} not in items, items
    assert items[-1] == {"content": "new"}


def test_broadcast_contract_event_delivered_under_overflow():
    """队列满时广播契约事件：契约事件本身必须送达（淘汰最旧 delta）。"""
    st, q = _full_state([{"content": "a"}, {"content": "b"}, {"content": "c"}])
    evt = {"interjection_committed": {"content": "x", "id": "m2"}}

    asyncio.run(st.broadcast(evt))

    items = _drain(q)
    assert items[-1] == evt, items
    assert {"content": "a"} not in items, items
    assert len(items) == 3


def test_broadcast_all_contract_overflow_stays_bounded_and_delivers_newest():
    """全契约项的退化场景：仍有界，且最新契约事件必达（不静默丢新信号）。"""
    st, q = _full_state([
        {"done": {"message_id": "m1"}},
        {"error": {"error": "e1"}},
        {"interjection_committed": {"content": "c1", "id": "i1"}},
    ])

    newest = {"interjection_committed": {"content": "c2", "id": "i2"}}
    asyncio.run(st.broadcast(newest))

    items = _drain(q)
    assert items[-1] == newest, items
    assert len(items) == 3


def test_broadcast_overflow_preserves_producer_terminal_and_failure_shapes():
    """真实广播形态：终态 `_completed` / 失败 `_failed` 也是不可丢契约事件。"""
    st, q = _full_state([
        {"_completed": {"message_id": "m1"}},
        {"_failed": "buffer create failed"},
        {"content": "a"},
    ])

    asyncio.run(st.broadcast({"content": "new"}))

    items = _drain(q)
    assert any("_completed" in e for e in items), items
    assert any("_failed" in e for e in items), items
    assert {"content": "a"} not in items, items
    assert len(items) == 3


def test_broadcast_overflow_preserves_permission_request():
    st, q = _full_state([
        {"permission_request": {"request_id": "r1"}},
        {"content": "a"},
    ])

    asyncio.run(st.broadcast({"content": "new"}))

    items = _drain(q)
    assert any("permission_request" in e for e in items), items
    assert {"content": "a"} not in items, items


def test_broadcast_done_evicts_delta_not_unconfirmed_echo():
    """终态哨兵入队同样不得牺牲未确认的插话 echo。"""
    st, q = _full_state([
        {"interjection_committed": {"content": "c1", "id": "i1"}},
        {"content": "a"},
        {"content": "b"},
    ])

    asyncio.run(st.broadcast_done())

    items = _drain(q)
    assert any("interjection_committed" in e for e in items), items
    assert {"content": "a"} not in items, items
    assert not isinstance(items[-1], dict), items


# ── B2 replay 携带插话 echo ────────────────────────────────────────────────

ECHO = {
    "id": "msg-1",
    "conversation_id": "conv-w26",
    "content": "运行中插话",
    "created_at": "2026-10-18T00:00:00",
    "client_message_id": "cm-1",
}


def test_buffer_records_interjection_for_replay_and_snapshot():
    buf = ConversationBuffer(conversation_id="conv-w26", user_id=1)

    asyncio.run(buf.append({"interjection_committed": ECHO}))

    assert buf.interjections_committed == [ECHO]
    assert buf.to_replay()["interjections_committed"] == [ECHO]
    assert buf.to_snapshot()["interjections_committed"] == [ECHO]


def test_buffer_broadcasts_interjection_delta_with_own_type():
    async def scenario():
        buf = ConversationBuffer(conversation_id="conv-w26", user_id=1)
        q, _snapshot = await buf.subscribe_with_snapshot()
        await buf.append({"interjection_committed": ECHO})
        return await asyncio.wait_for(q.get(), timeout=1.0)

    delta = asyncio.run(scenario())
    assert delta["type"] == "interjection_committed", delta
    assert delta["data"] == ECHO, delta


def test_snapshot_loop_persists_interjections(monkeypatch):
    from app.services import stream_buffer as sb

    captured = {}

    async def fake_save(cid, snap):
        captured[cid] = snap

    monkeypatch.setattr(sb.shared_state, "save_buffer_state", fake_save)

    async def scenario():
        mgr = StreamBufferManager()
        buf = ConversationBuffer(conversation_id="conv-w26", user_id=1)
        buf.interjections_committed = [ECHO]
        mgr._buffers["conv-w26"] = buf
        await mgr._snapshot_running_buffers()

    asyncio.run(scenario())
    assert captured["conv-w26"]["interjections_committed"] == [ECHO], captured


# ── B3 client_message_id 精确配对 ─────────────────────────────────────────

def _loop() -> AgentLoop:
    loop = object.__new__(AgentLoop)
    loop.deathmatch_manager = None
    return loop


def test_drain_dict_item_carries_client_message_id():
    state = AgentLoopState(messages=[])
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait({"content": "插话一", "client_message_id": "cm-1"})
    state.interjection_queue = q

    events = _loop()._drain_interjections(state)

    assert events == [{
        "interjection_committed": {"content": "插话一", "client_message_id": "cm-1"}
    }]
    assert state.messages[-1] == {"role": "user", "content": "插话一", "_interjection": True}


def test_drain_dict_item_without_id_keeps_legacy_shape():
    state = AgentLoopState(messages=[])
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait({"content": "插话二"})
    state.interjection_queue = q

    events = _loop()._drain_interjections(state)

    assert events == [{"interjection_committed": {"content": "插话二"}}]


def test_drain_legacy_string_item_unchanged():
    state = AgentLoopState(messages=[])
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait("旧投递（agent_worker 字符串队列）")
    state.interjection_queue = q

    events = _loop()._drain_interjections(state)

    assert events == [{"interjection_committed": {"content": "旧投递（agent_worker 字符串队列）"}}]
    assert state.messages[-1]["content"] == "旧投递（agent_worker 字符串队列）"


def test_drain_skips_empty_dict_item():
    state = AgentLoopState(messages=[])
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait({"content": "   "})
    q.put_nowait({"content": "有效", "client_message_id": "cm-2"})
    state.interjection_queue = q

    events = _loop()._drain_interjections(state)

    assert [e["interjection_committed"]["content"] for e in events] == ["有效"]
    assert len(state.messages) == 1


def test_interject_request_normalization():
    from app.api.chat import _normalize_interject_request

    assert _normalize_interject_request({"content": "  hi  ", "client_message_id": " cm-1 "}) == (
        "hi", "cm-1"
    )
    assert _normalize_interject_request({"content": "hi"}) == ("hi", None)
    assert _normalize_interject_request({"content": "hi", "client_message_id": ""}) == ("hi", None)
    # 非字符串不 str() 强转（否则 dict 会变成 Python repr 注入用户文本）
    assert _normalize_interject_request({"content": {"a": 1}}) == ("", None)
    assert _normalize_interject_request({"content": "hi", "client_message_id": {"a": 1}}) == ("hi", None)
    # 超长 id 不在 helper 里静默截断（端点 400，客户端可重试）
    long_id = "x" * 300
    assert _normalize_interject_request({"content": "hi", "client_message_id": long_id})[1] == long_id


def test_interject_endpoint_rejects_overlong_client_message_id():
    from app.api import chat as chat_module

    src = open(chat_module.__file__, encoding="utf-8").read()
    assert "client_message_id 过长" in src
    assert "_MAX_INTERJECT_CLIENT_MESSAGE_ID" in src


def test_interject_endpoint_queues_struct_payload():
    """接线钉：端点必须把 cmid 随内容入队（否则 echo 永远拿不到 id）。"""
    from app.api import chat as chat_module

    src = open(chat_module.__file__, encoding="utf-8").read()
    assert '"client_message_id": client_message_id' in src
    assert "_normalize_interject_request(request_data)" in src
