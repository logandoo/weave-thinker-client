"""RED tests (A4.8) — T3b：task 死亡后 done-callback 兜底释放槽位。

背景（conv 5228db60 事故）：`task.cancel()` 落在 `_agent_loop_task` 主
try/finally（unregister）之前 11ms 窗口 → 三行清理日志 0 行 → adopted 槽
永生。修复契约（①③）：
  - `_track_detached_task` 的 done callback 兜底调用
    `release_if_task_dead(conv_id, expected_task=task)`；
  - 释放语义 identity 校验（expected_task 必须等于绑定 task_handle）；
  - task 未 done / identity 不符 → 绝不动槽。

Run: cd backend && python -m pytest tests/test_stop_cancel_slot_release.py -v
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

from app.services.active_agent_registry import (  # noqa: E402
    ActiveAgentRegistry,
    ActiveAgentState,
)


def test_release_if_task_dead_frees_dead_adopted_slot():
    reg = ActiveAgentRegistry()
    cid = "conv-a"
    st = _run(reg.reserve(cid))
    assert st is not None
    assert _run(reg.adopt_reservation(cid, st)) is st

    async def _noop():
        return None

    t = _spawn(_noop())
    _run(asyncio.wait_for(t, 5))
    st.task_handle = t

    freed = _run(reg.release_if_task_dead(cid, expected_task=t))
    assert freed is True
    assert reg.get_local(cid) is None


def test_release_if_task_dead_keeps_live_slot():
    reg = ActiveAgentRegistry()
    cid = "conv-b"
    st = _run(reg.reserve(cid))
    _run(reg.adopt_reservation(cid, st))

    async def _hang():
        await asyncio.sleep(30)

    t = _spawn(_hang())
    st.task_handle = t
    try:
        freed = _run(reg.release_if_task_dead(cid, expected_task=t))
        assert freed is False
        assert reg.get_local(cid) is st
    finally:
        t.cancel()


def test_release_if_task_dead_identity_mismatch_never_pops():
    reg = ActiveAgentRegistry()
    cid = "conv-c"
    st = _run(reg.reserve(cid))
    _run(reg.adopt_reservation(cid, st))

    async def _hang():
        await asyncio.sleep(30)

    owner = _spawn(_hang())
    st.task_handle = owner

    async def _noop():
        return None

    stranger = _spawn(_noop())
    _run(asyncio.wait_for(stranger, 5))
    try:
        freed = _run(reg.release_if_task_dead(cid, expected_task=stranger))
        assert freed is False
        assert reg.get_local(cid) is st
    finally:
        owner.cancel()


def test_reaper_sweeps_orphaned_adopted_entry_but_not_fresh_or_live():
    reg = ActiveAgentRegistry()
    import time as _t

    cid_dead = "conv-dead-adopted"
    st = reg.register_now(cid_dead) if hasattr(reg, "register_now") else None
    # 兼容最小缝：直接构造条目（字段均为 dataclass 公开字段）
    from app.services.active_agent_registry import ActiveAgentState

    st = ActiveAgentState(conversation_id=cid_dead)
    st.is_running = True
    st.provisional = False
    st.reserved_at = _t.time() - 310
    st.task_handle = None
    reg._agents[cid_dead] = st

    cid_fresh = "conv-fresh-adopted"
    st2 = ActiveAgentState(conversation_id=cid_fresh)
    st2.is_running = True
    st2.provisional = False
    st2.reserved_at = _t.time() - 10
    st2.task_handle = None
    reg._agents[cid_fresh] = st2

    cid_live = "conv-live-crossworker"
    st3 = ActiveAgentState(conversation_id=cid_live)
    st3.is_running = True
    st3.provisional = False
    st3.reserved_at = _t.time() - 310
    st3.task_handle = None
    st3.recovered_cross_worker = True  # 跨 worker 活 run 的本地镜像，不得误杀
    reg._agents[cid_live] = st3

    _run(reg.sweep_orphans())

    assert reg.get_local(cid_dead) is None, "死槽（无 task 超时 adopted）应被回收"
    assert reg.get_local(cid_fresh) is st2, "新鲜条目不得回收"
    assert reg.get_local(cid_live) is st3, "跨 worker 活 run 镜像不得误杀"


def test_detached_done_callback_releases_orphan_slot():
    """A4.9 R1 #4：①接线级——_track_detached_task 的 done 兜底真实释放死槽
    （PLAN Task3 指定缝：假 task + 注册表状态 → 等待 done → 断言释放）。"""
    import time as _t

    from app.api import chat as chat_mod

    reg = chat_mod._agent_registry
    cid = "conv-wire-orphan"
    st = ActiveAgentState(conversation_id=cid)
    st.is_running = True
    st.provisional = False
    st.reserved_at = _t.time()
    reg._agents[cid] = st

    async def _noop():
        return None

    try:
        t = _spawn(_noop())
        _run(asyncio.wait_for(asyncio.gather(t, return_exceptions=True), 5))
        st.task_handle = t
        chat_mod._track_detached_task(t, cid)
        _run(asyncio.sleep(0.1))
        assert reg.get_local(cid) is None, "done 兜底应释放绑定死槽"
        assert cid not in chat_mod._DETACHED_AGENT_TASKS
    finally:
        reg._agents.pop(cid, None)
        chat_mod._DETACHED_AGENT_TASKS.pop(cid, None)


def test_on_done_identity_guard_keeps_newer_task_entry():
    """A4.9 R1 #5：supersede 场景旧 task 回调不得 pop 新 run 的条目/释放新槽。"""
    import time as _t

    from app.api import chat as chat_mod

    cid = "conv-wire-supersede"
    reg = chat_mod._agent_registry
    st = ActiveAgentState(conversation_id=cid)
    st.is_running = True
    st.provisional = False
    st.reserved_at = _t.time()

    async def _hang():
        await asyncio.sleep(30)

    async def _noop():
        return None

    t_old = _spawn(_noop())
    _run(asyncio.wait_for(asyncio.gather(t_old, return_exceptions=True), 5))
    t_new = _spawn(_hang())
    try:
        chat_mod._track_detached_task(t_old, cid)      # dict[cid]=t_old，done 回调排队
        chat_mod._DETACHED_AGENT_TASKS[cid] = t_new    # 模拟新 run 覆盖注册
        st.task_handle = t_new
        reg._agents[cid] = st
        _run(asyncio.sleep(0.1))
        assert chat_mod._DETACHED_AGENT_TASKS.get(cid) is t_new, "旧回调不得 pop 新条目"
        assert reg.get_local(cid) is st, "identity 不符不得释放新槽"
    finally:
        t_new.cancel()
        reg._agents.pop(cid, None)
        chat_mod._DETACHED_AGENT_TASKS.pop(cid, None)
