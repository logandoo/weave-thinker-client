"""P1 S5: message deletion invalidates the anchored rolling summary."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.context_continuity_service import invalidate_continuity


class _FakeDB:
    def __init__(self):
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))


def test_invalidate_continuity_deletes_summary_and_snapshot_rows():
    db = _FakeDB()
    asyncio.run(invalidate_continuity(db, "conv-1"))
    sql = "\n".join(s for s, _ in db.calls)
    assert "conversation_rolling_summaries" in sql
    assert "conversation_resume_snapshots" in sql
    assert all(p == {"cid": "conv-1"} for _, p in db.calls)


def test_delete_message_slice_invalidates_continuity():
    import asyncio
    from types import SimpleNamespace
    from app.api.chat_persistence import _delete_message_slice

    class _Result:
        """P0 方案 A（2026-10-31 UPSTREAM_TODO_20261031）：切片删除改为
        「按 id 重查 ORM 实例逐行 db.delete + 显式 flush」——fake 需返回可
        `.scalars().all()` 的结果并提供 delete/flush（工单 §2.2 明确要求）。"""
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return SimpleNamespace(all=lambda: list(self._rows))

    class _DB:
        def __init__(self):
            self.calls = []
            self.deleted = []
            self.flushed = 0

        async def execute(self, statement, params=None):
            self.calls.append((str(statement), params))
            return _Result(getattr(self, "orm_rows", []))

        async def delete(self, obj):
            self.deleted.append(obj)

        async def flush(self):
            self.flushed += 1

    db = _DB()
    rows = [SimpleNamespace(id="m1", conversation_id="c1")]
    db.orm_rows = rows
    asyncio.run(_delete_message_slice(db, rows, user_id=None))
    assert db.deleted == rows, "切片行必须经 ORM db.delete 逐行删除（mapper 事件面）"
    assert db.flushed >= 1, "必须显式 flush——delete 事件 seq 要早于新消息 create"
    sql = "\n".join(s for s, _ in db.calls)
    assert "conversation_rolling_summaries" in sql
    assert "conversation_resume_snapshots" in sql
