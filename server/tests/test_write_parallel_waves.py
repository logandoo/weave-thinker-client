"""T2 P1 — per-path 写锁 + 同路径分波并行（criteria 1）。

测试面（docs/PLAN_durable_execution_wave.md T2）：
- partition_write_waves 纯函数：同路径分属不同波且保序、异路径同波、无 path 不冲突
- path_lock：同路径互斥（探针无交错）、异路径可并行（墙钟 < 串行 0.5x 量级）
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _tc(name, call_id, path=None):
    fn = {"name": name, "arguments": {}}
    if path is not None:
        fn["arguments"] = {"path": path}
    return {"id": call_id, "type": "function", "function": fn}


def test_partition_same_path_serialized_in_order():
    from app.services.workspace_lock_service import partition_write_waves

    calls = [
        _tc("workspace_edit", "1", path="a.md"),
        _tc("workspace_edit", "2", path="a.md"),
        _tc("workspace_write", "3", path="b.md"),
    ]
    waves = partition_write_waves(calls)
    assert [tc["id"] for tc in waves[0]] == ["1", "3"], "异路径同波"
    assert [tc["id"] for tc in waves[1]] == ["2"], "同路径挤到下一波"
    # 同路径保序语义：id1 的波次索引 < id2 的波次索引（结果回填按模型序）
    wave_of = {tc["id"]: i for i, wave in enumerate(waves) for tc in wave}
    assert wave_of["1"] < wave_of["2"]


def test_partition_no_path_tools_never_conflict():
    from app.services.workspace_lock_service import partition_write_waves

    calls = [
        _tc("grep", "1"),
        _tc("workspace_edit", "2", path="a.md"),
        _tc("grep", "3"),
    ]
    waves = partition_write_waves(calls)
    assert len(waves) == 1
    assert [tc["id"] for tc in waves[0]] == ["1", "2", "3"]


def test_partition_empty_single_all_conflict():
    from app.services.workspace_lock_service import partition_write_waves

    assert partition_write_waves([]) == []
    single = partition_write_waves([_tc("workspace_write", "1", path="x")])
    assert [[tc["id"] for tc in w] for w in single] == [["1"]]
    conflict = partition_write_waves([
        _tc("workspace_edit", "1", path="a"),
        _tc("workspace_edit", "2", path="a"),
        _tc("workspace_edit", "3", path="a"),
    ])
    assert [[tc["id"] for tc in w] for w in conflict] == [["1"], ["2"], ["3"]]


def test_path_lock_same_path_no_interleaving():
    from app.services.workspace_lock_service import path_lock

    async def main():
        lock_a = path_lock("u1", "a.md")
        lock_b = path_lock("u1", "a.md")
        assert lock_a is lock_b, "同 user+path 必须同一把锁"
        events = []

        async def writer(tag, delay):
            async with lock_a:
                events.append(f"{tag}:enter")
                await asyncio.sleep(delay)
                events.append(f"{tag}:exit")

        await asyncio.gather(writer("A", 0.05), writer("B", 0.01))
        return events

    events = asyncio.run(main())
    assert events in (["A:enter", "A:exit", "B:enter", "B:exit"],
                      ["B:enter", "B:exit", "A:enter", "A:exit"]), f"同路径出现交错: {events}"


def test_path_lock_different_paths_run_concurrently():
    from app.services.workspace_lock_service import path_lock

    async def main():
        la = path_lock("u1", "a.md")
        lb = path_lock("u1", "b.md")

        async def writer(lock):
            async with lock:
                await asyncio.sleep(0.3)

        t0 = time.monotonic()
        await asyncio.gather(writer(la), writer(lb))
        return time.monotonic() - t0

    elapsed = asyncio.run(main())
    assert elapsed < 0.45, f"异路径未并行（{elapsed:.2f}s ≥ 串行 0.6s 的 0.5x 阈值）"


def test_path_lock_user_isolation():
    from app.services.workspace_lock_service import path_lock

    assert path_lock("u1", "a.md") is not path_lock("u2", "a.md")
