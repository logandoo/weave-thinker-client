"""TDD tests — consolidation 合并项 savepoint 隔离 + episode 空数组合并（2026-09-22）。

生产 user d2998b76：episode 合并的 `jsonb_agg` 在双方 source_unit_ids='[]' 时
聚合出 NULL → NOT NULL 违约；DEBUG 级吞异常且无 savepoint → 事务中毒 →
下一站 rebalance 误报 InFailedSQLTransactionError（整轮回滚）。
修复：SQL COALESCE+NULLIF + per-item savepoint（失败项只回滚自身）+ 日志升 warning。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_consolidation_merge_isolation.py -v
"""
import asyncio
import logging

from app.services.memory_consolidation_service import (
    _EPISODE_MERGE_CONTENT_SQL,
    _isolated_merge_item,
)


class _Nested:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        self.db.nested_entered += 1
        return None

    async def __aexit__(self, *exc):
        self.db.nested_exited += 1
        return False  # do not suppress — the helper's except does the isolation


class _FakeDB:
    def __init__(self):
        self.nested_entered = 0
        self.nested_exited = 0
        self.executed = []

    def begin_nested(self):
        return _Nested(self)

    async def execute(self, stmt, params=None):
        self.executed.append((stmt, params))
        return None


class TestEpisodeMergeSql:
    def test_empty_union_coalesces_to_empty_array(self):
        sql = str(_EPISODE_MERGE_CONTENT_SQL)
        # both sides '[]' → jsonb_agg over zero rows is NULL → must coalesce
        assert "COALESCE(jsonb_agg(DISTINCT x), '[]'::jsonb)" in sql
        # empty-string text (latent cast error) must be normalized away
        assert "NULLIF(source_unit_ids, '')::jsonb" in sql
        # still the same update target/params contract
        assert "UPDATE memory_episodes" in sql and ":kept" in sql and ":merged" in sql


class TestIsolatedMergeItem:
    def test_success_runs_in_savepoint_and_returns_true(self):
        db = _FakeDB()

        async def ok():
            await db.execute("UPDATE x")
            return True

        assert asyncio.run(_isolated_merge_item(db, "ok-item", ok)) is True
        assert db.nested_entered == 1 and db.nested_exited == 1

    def test_failure_is_isolated_logged_and_does_not_stop_following_items(self, caplog):
        db = _FakeDB()

        async def boom():
            raise RuntimeError("simulated NotNullViolation")

        async def ok():
            return True

        with caplog.at_level(logging.WARNING, logger="app.services.memory_consolidation_service"):
            assert asyncio.run(_isolated_merge_item(db, "bad-item", boom)) is False
            assert asyncio.run(_isolated_merge_item(db, "ok-item", ok)) is True
        assert "bad-item" in caplog.text
        # both items were attempted under their own savepoint
        assert db.nested_entered == 2 and db.nested_exited == 2

    def test_falsy_factory_result_reports_false(self):
        db = _FakeDB()

        async def nothing_to_merge():
            return False

        assert asyncio.run(_isolated_merge_item(db, "skip-item", nothing_to_merge)) is False
        assert db.nested_entered == 1
