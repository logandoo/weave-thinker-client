"""RED tests (A4.8) — T3a：interject 对死槽返回 not_running（不再静默吞消息）。

背景（conv 5228db60 事故）：task 死亡后 adopted registry 槽 is_running=True
永生 → interject 检查 `state.is_running` 即把用户消息排进死队列的
interjection_queue，commit-time 落库语义 = 永不消费即永不落库 → 用户 58 字
消息静默蒸发。修复契约：目标 task 已死（task_handle.done()）→ 返回既有三态
之一 `not_running`，客户端回退普通发送（撞 conversation_busy 有可见反馈），
绝不吞进死队列。

Run: cd backend && python -m pytest tests/test_interject_dead_claim.py -v
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


_LOOP = None

def _run(coro):
    """pytest 同步跑协程（3.13 get_event_loop 在无循环时 RuntimeError）。"""
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _spawn(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.create_task(coro)

from app.api import chat as chat_mod  # noqa: E402
from app.services.active_agent_registry import ActiveAgentState  # noqa: E402

_interject_target_alive = getattr(chat_mod, "_interject_target_alive", None)


def _dead_task():
    async def _noop():
        return None

    t = _spawn(_noop())
    return t


def test_dead_task_claim_is_not_alive():
    assert callable(_interject_target_alive), "missing _interject_target_alive (T3a 未实现)"
    st = ActiveAgentState(conversation_id="c1")
    st.is_running = True
    t = _dead_task()
    _run(asyncio.wait_for(t, 5))
    st.task_handle = t
    assert _interject_target_alive(st) is False


def test_live_task_claim_is_alive():
    st = ActiveAgentState(conversation_id="c1")
    st.is_running = True

    async def _hang():
        await asyncio.sleep(30)

    t = _spawn(_hang())
    st.task_handle = t
    try:
        assert _interject_target_alive(st) is True
    finally:
        t.cancel()


def test_provisional_no_handle_claim_stays_alive():
    """setup 期（provisional=True 无句柄）保留旧行为——插话可排队给即将启动的 run。"""
    st = ActiveAgentState(conversation_id="c1")
    st.is_running = True
    st.task_handle = None
    st.provisional = True
    assert _interject_target_alive(st) is True


def test_adopted_no_handle_claim_is_dead():
    """A4.9 R1 #3：adopted 无句柄 running-claim = 死槽（PLAN Task3 ⑤ 原契约）。"""
    st = ActiveAgentState(conversation_id="c1")
    st.is_running = True
    st.task_handle = None
    st.provisional = False
    assert _interject_target_alive(st) is False
