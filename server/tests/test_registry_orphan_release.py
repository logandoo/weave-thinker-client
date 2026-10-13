"""RED tests (A4.8) — T2：registry 死任务释放原语 + reaper 扩展（038e837a 遗留③）。

修复契约：
  - `ActiveAgentState.task_handle: asyncio.Task | None`（绑定本进程 agent task）；
  - `release_if_task_dead(conversation_id, expected_task=None) -> bool`：
    identity 校验 + task 已 done（或本无 task）→ 置 is_running=False 并 pop；
  - `sweep_orphans()`：回收 `is_running and not provisional and
    (task_handle 为空或已 done) and now-reserved_at > _ORPHAN_ADOPTED_TTL_SECONDS`
    的条目；`recovered_cross_worker=True` 的本地镜像（跨 worker 活 run）绝不回收。

Run: cd backend && python -m pytest tests/test_registry_orphan_release.py -v
"""
import asyncio
import os
import sys
import time

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

from app.services import active_agent_registry as reg_mod  # noqa: E402
from app.services.active_agent_registry import (  # noqa: E402
    ActiveAgentRegistry,
    ActiveAgentState,
)

_ORPHAN_ADOPTED_TTL_SECONDS = getattr(reg_mod, "_ORPHAN_ADOPTED_TTL_SECONDS", 300)


def _adopted(cid, age=0.0, task=None, cross=False):
    st = ActiveAgentState(conversation_id=cid)
    st.is_running = True
    st.provisional = False
    st.reserved_at = time.time() - age
    st.task_handle = task
    st.recovered_cross_worker = cross
    return st


def _done_task():
    async def _noop():
        return None

    t = _spawn(_noop())
    _run(asyncio.wait_for(t, 5))
    return t


def test_release_dead_task_slot():
    assert hasattr(ActiveAgentRegistry, "release_if_task_dead"), "missing release_if_task_dead (T2 未实现)"
    assert hasattr(ActiveAgentState, "task_handle"), "missing task_handle (T2 未实现)"
    reg = ActiveAgentRegistry()
    st = _adopted("c1", task=None)
    t = _done_task()
    st.task_handle = t
    reg._agents["c1"] = st
    assert _run(
        reg.release_if_task_dead("c1", expected_task=t)
    ) is True
    assert reg.get_local("c1") is None
    assert st.is_running is False


def test_release_keeps_live_task_slot():
    reg = ActiveAgentRegistry()

    async def _hang():
        await asyncio.sleep(30)

    t = _spawn(_hang())
    st = _adopted("c2", task=t)
    reg._agents["c2"] = st
    try:
        assert _run(
            reg.release_if_task_dead("c2", expected_task=t)
        ) is False
        assert reg.get_local("c2") is st
    finally:
        t.cancel()


def test_release_identity_mismatch_never_pops():
    reg = ActiveAgentRegistry()

    async def _hang():
        await asyncio.sleep(30)

    owner = _spawn(_hang())
    st = _adopted("c3", task=owner)
    reg._agents["c3"] = st
    stranger = _done_task()
    try:
        assert _run(
            reg.release_if_task_dead("c3", expected_task=stranger)
        ) is False
        assert reg.get_local("c3") is st
    finally:
        owner.cancel()


def test_sweep_orphan_adopted_three_way():
    assert hasattr(ActiveAgentRegistry, "sweep_orphans"), "missing sweep_orphans (T2 未实现)"
    reg = ActiveAgentRegistry()
    dead = _adopted("dead", age=_ORPHAN_ADOPTED_TTL_SECONDS + 10)
    fresh = _adopted("fresh", age=10)
    cross = _adopted("cross", age=_ORPHAN_ADOPTED_TTL_SECONDS + 10, cross=True)
    reg._agents["dead"] = dead
    reg._agents["fresh"] = fresh
    reg._agents["cross"] = cross

    _run(reg.sweep_orphans())

    assert reg.get_local("dead") is None
    assert reg.get_local("fresh") is fresh
    assert reg.get_local("cross") is cross


def test_sweep_dead_task_adopted_entry():
    """task 已 done 的 adopted 条目超时后同样被回收（即使句柄仍在）。"""
    reg = ActiveAgentRegistry()
    t = _done_task()
    st = _adopted("d2", age=_ORPHAN_ADOPTED_TTL_SECONDS + 10, task=t)
    reg._agents["d2"] = st
    _run(reg.sweep_orphans())
    assert reg.get_local("d2") is None


def test_sweep_dead_owner_mirror_but_not_live_owner(monkeypatch):
    """A4.9 R1 #2：跨 worker 镜像按 owner 心跳复核——owner 死可回收（含 shared 行），
    owner 活绝不回收；心跳检查失败按存活处理（fail-safe）。"""
    from app.services import active_agent_registry as reg_mod

    reg = ActiveAgentRegistry()
    shared_calls = []

    async def _fake_active(wid):
        return wid != "dead-worker"

    async def _fake_delete(cid):
        shared_calls.append(cid)

    monkeypatch.setattr(type(reg_mod.shared_state), "is_db_enabled", property(lambda self: True))
    monkeypatch.setattr(reg_mod.shared_state, "is_worker_active", _fake_active)
    monkeypatch.setattr(reg_mod.shared_state, "delete_agent_state", _fake_delete)

    ttl = _ORPHAN_ADOPTED_TTL_SECONDS
    dead = _adopted("m-dead", age=ttl + 10, cross=True)
    dead.owner_worker_id = "dead-worker"
    live = _adopted("m-live", age=ttl + 10, cross=True)
    live.owner_worker_id = "live-worker"
    reg._agents["m-dead"] = dead
    reg._agents["m-live"] = live

    n = _run(reg.sweep_orphans())

    assert n == 1
    assert reg.get_local("m-dead") is None
    assert reg.get_local("m-live") is live
    assert shared_calls == ["m-dead"], "被回收镜像的 shared 行必须同删（A4.9 R1 #1）"
