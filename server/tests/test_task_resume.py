"""T4 D-轻 — agent_tasks checkpoint/resume + 副作用台账（criteria 3）。

测试面（docs/PLAN_durable_execution_wave.md T4）：
- 台账幂等：同 cursor+tool 二次执行跳过（begin 冲突 → should_skip True，
  get_result 返回已记录结果——不重放副作用）
- checkpoint 序列化 roundtrip + 版本>1 loud error + 体积上限截最旧并披露计数
- plan_resume：从 checkpoint 重建 (messages, budget_used)——State-Aware 恢复语义
- IterationBudget(preused=)：消费计数从既有值继续
- recovery_status / drain_status_values：有 checkpoint → resumable，无 → failed
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class MemoryLedgerStore:
    def __init__(self):
        self.rows = {}

    async def get(self, key):
        return self.rows.get(key)

    async def insert(self, record):
        key = record["idempotency_key"]
        if key in self.rows:
            return False
        self.rows[key] = dict(record)
        return True

    async def update(self, key, status, result_ref=None):
        row = self.rows.get(key)
        if row:
            row["status"] = status
            row["result_ref"] = result_ref

    async def update_if(self, key, expected_status, status):
        row = self.rows.get(key)
        if row is None or row.get("status") != expected_status:
            return False
        row["status"] = status
        row["result_ref"] = None
        return True


def test_ledger_skips_second_execution():
    from app.services import side_effect_ledger as sel

    async def main():
        store = MemoryLedgerStore()
        p = ("agent_task", "t1")
        assert await sel.should_skip(store, *p, 3, "workspace_write", "c1") is False
        key = await sel.begin(store, *p, 3, "workspace_write", "c1")
        assert key == "agent_task:t1:3:workspace_write:c1"
        await sel.finish(store, key, "done", "写入完成")
        # 同 call 重放 = 跳过 + 拿到已记录结果
        assert await sel.should_skip(store, *p, 3, "workspace_write", "c1") is True
        assert await sel.get_result(store, *p, 3, "workspace_write", "c1") == "写入完成"
        # 不同 call（同 cursor+tool，A4.9 R1 C3 场景）= 不冲突，必须都执行
        assert await sel.should_skip(store, *p, 3, "workspace_write", "c2") is False
        assert await sel.should_skip(store, *p, 4, "workspace_write", "c1") is False
        assert await sel.should_skip(store, *p, 3, "notes", "c1") is False
        return await sel.begin(store, *p, 3, "workspace_write", "c1")

    second_begin = asyncio.run(main())
    assert second_begin is None, "同 call 重复 begin 必须返回 None（=已记录，应跳过）"


def test_ledger_same_tool_same_iteration_two_calls_both_run():
    """A4.9 R1 C3 回归：P1 异文件并行下同轮两次 workspace_write 不能互相吞掉。"""
    from app.services import side_effect_ledger as sel

    async def main():
        store = MemoryLedgerStore()
        k1 = await sel.begin(store, "agent_task", "t9", 3, "workspace_write", "callA")
        k2 = await sel.begin(store, "agent_task", "t9", 3, "workspace_write", "callB")
        return k1, k2

    k1, k2 = asyncio.run(main())
    assert k1 is not None and k2 is not None, "两次调用都必须获得执行权"
    assert k1 != k2


def test_ledger_pending_before_finish():
    from app.services import side_effect_ledger as sel

    async def main():
        store = MemoryLedgerStore()
        key = await sel.begin(store, "agent_task", "t2", 1, "terminal", "cX")
        # pending 也算已记录（write-before-invoke：崩溃于执行中不重复执行，置 unknown）
        assert await sel.should_skip(store, "agent_task", "t2", 1, "terminal", "cX") is True
        return key

    assert asyncio.run(main()) == "agent_task:t2:1:terminal:cX"


def test_checkpoint_roundtrip():
    from app.services.task_checkpoint import build_checkpoint, parse_checkpoint, plan_resume

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "做任务"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "grep", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "grep", "content": "结果"},
    ]
    cp = build_checkpoint(cursor=5, messages=msgs, budget_used=5, elapsed=12.5)
    assert cp["version"] == 1 and cp["cursor"] == 5 and cp["budget_used"] == 5
    raw = json.dumps(cp, ensure_ascii=False)
    cp2 = parse_checkpoint(raw)
    restored, budget_used = plan_resume(cp2)
    assert budget_used == 5
    assert [m["role"] for m in restored] == ["system", "user", "assistant", "tool"]
    assert restored[2]["tool_calls"][0]["function"]["name"] == "grep"


def test_checkpoint_version_loud_error():
    from app.services.task_checkpoint import parse_checkpoint

    try:
        parse_checkpoint(json.dumps({"version": 2, "cursor": 0, "messages": [], "budget_used": 0}))
        result = "no-error"
    except ValueError:
        result = "value-error"
    assert result == "value-error", "回滚遇新版本 checkpoint 必须 loud error"


def test_checkpoint_size_cap_disclosed():
    from app.services.task_checkpoint import build_checkpoint

    msgs = [{"role": "user", "content": "x" * 100_000} for _ in range(40)]
    cp = build_checkpoint(cursor=1, messages=msgs, budget_used=1, max_bytes=300_000)
    raw = json.dumps(cp, ensure_ascii=False)
    assert len(raw) < 500_000
    assert cp.get("dropped_messages", 0) > 0, "截断必须显式披露计数"
    assert cp["messages"], "保留最新消息"


def test_iteration_budget_preused():
    from app.services.agent_loop import IterationBudget

    async def main():
        b = IterationBudget(50, preused=40)
        assert b.used == 40
        assert await b.get_remaining() == 10
        assert await b.consume() is True
        assert b.used == 41
        return await b.get_remaining()

    assert asyncio.run(main()) == 9


def test_recovery_and_drain_status():
    from app.services.task_checkpoint import recovery_status, drain_status_values

    assert recovery_status(True) == "resumable"
    assert recovery_status(False) == "failed"
    d1 = drain_status_values(has_checkpoint=True)
    d2 = drain_status_values(has_checkpoint=False)
    assert d1["status"] == "resumable" and d1["completed_at"] is None
    assert d2["status"] == "failed" and d2["completed_at"] is not None
