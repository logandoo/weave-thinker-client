"""RED tests (A4.8) — T1：AgentTask.worker_id 补进 ORM（durable 波回归）。

背景：2026-09-23 00:04 部署 8c1f065f8 后 `_recover_orphaned_tasks`
（app/services/agent_worker.py:118）引用 `AgentTask.worker_id`，ORM 无此属性
→ AttributeError → 每 60s "Failed to recover orphaned agent tasks"，
孤儿恢复网全天下线（60 生产 chatllm.log 实证 35+ 次）。
DB 列 `agent_tasks.worker_id` 已存在（durable 波迁移加列），仅缺 ORM 声明。

Run: cd backend && python -m pytest tests/test_agent_task_worker_id.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import or_  # noqa: E402

from app.db.database import AgentTask  # noqa: E402


def test_agent_task_has_worker_id_attribute():
    assert getattr(AgentTask, "worker_id") is not None


def test_agent_task_worker_id_compiles_in_recovery_query():
    """agent_worker._recover_orphaned_tasks 的条件构造不得再 AttributeError。"""
    cond = or_(AgentTask.worker_id.is_(None), AgentTask.worker_id.notin_(["x"]))
    compiled = str(cond.compile(compile_kwargs={"literal_binds": True}))
    assert "worker_id" in compiled
