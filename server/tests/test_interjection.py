"""插话（interjection）迭代边界 drain 单元测试。

设计来源（2026-08-29）：codex pending_input drain / deepseek-harness next-step
inbox / opencode steer inbox 三参考共同模式——运行中 run 的用户插话进入
per-conversation 队列，在 _run_loop 每轮迭代顶部注入为真实 user 消息并 yield
interjection_committed 事件（commit-echo），不打断在途 token。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.agent_loop import AgentLoop, AgentLoopState  # noqa: E402


def _loop(deathmatch=None) -> AgentLoop:
    loop = object.__new__(AgentLoop)  # 纯逻辑单测，避开 __init__ 重依赖
    loop.deathmatch_manager = deathmatch
    return loop


def test_drain_appends_user_message_and_yields_event():
    state = AgentLoopState(messages=[{"role": "user", "content": "原始问题"}])
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait("插话一")
    state.interjection_queue = q
    events = _loop()._drain_interjections(state)
    assert events == [{"interjection_committed": {"content": "插话一"}}]
    assert state.messages[-1] == {"role": "user", "content": "插话一", "_interjection": True}
    assert q.empty()


def test_drain_multiple_fifo_order():
    state = AgentLoopState(messages=[])
    q: asyncio.Queue = asyncio.Queue()
    for t in ("第一条", "第二条", "第三条"):
        q.put_nowait(t)
    state.interjection_queue = q
    events = _loop()._drain_interjections(state)
    assert [e["interjection_committed"]["content"] for e in events] == ["第一条", "第二条", "第三条"]
    assert [m["content"] for m in state.messages] == ["第一条", "第二条", "第三条"]


def test_drain_empty_queue_noop():
    state = AgentLoopState(messages=[{"role": "user", "content": "原始问题"}])
    state.interjection_queue = asyncio.Queue()
    events = _loop()._drain_interjections(state)
    assert events == []
    assert len(state.messages) == 1


def test_drain_none_queue_noop():
    state = AgentLoopState(messages=[{"role": "user", "content": "原始问题"}])
    assert state.interjection_queue is None
    events = _loop()._drain_interjections(state)
    assert events == []
    assert len(state.messages) == 1


def test_deathmatch_run_never_drains():
    state = AgentLoopState(messages=[])
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait("不应注入")
    state.interjection_queue = q
    events = _loop(deathmatch=object())._drain_interjections(state)
    assert events == []
    assert state.messages == []
    assert not q.empty()  # 队列原样保留（端点侧本就拒收 deathmatch，此为双保险）
