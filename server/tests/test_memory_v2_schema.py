"""W0/F（记忆/长程 v2，2026-10-23）：§5 数据模型与迁移契约。

判据 #1：5 新表 + 5 列变更 + 2 组索引由启动迁移落库；backfill 默认 dry-run、
--execute 幂等。纯结构/源码断言（不连库）——运行时落库由启动迁移幂等保证，
守卫由 test_startup_migrations_sql.py 承担。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_memory_v2_schema.py -v
"""
import inspect

from app.db import migrations as m
from app.db.database import (
    AgentDream,
    Base,
    ConversationPinnedConstraint,
    DeletionManifest,
    DreamRepairReport,
    MemoryConcept,
    MemoryEvalRun,
    StepCheckpoint,
)


def _names():
    return [v for v, _ in m.STARTUP_MIGRATIONS]


def _stmts():
    return dict(m.STARTUP_MIGRATIONS)


class TestNewTables:
    def test_five_new_models_registered(self):
        tables = Base.metadata.tables
        for name in (
            "conversation_pinned_constraints",
            "deletion_manifests",
            "dream_repair_reports",
            "step_checkpoints",
            "memory_eval_runs",
        ):
            assert name in tables, f"缺 ORM 表 {name}"

    def test_pinned_constraints_columns(self):
        cols = ConversationPinnedConstraint.__table__.columns.keys()
        for c in ("user_id", "conversation_id", "text", "source_message_id",
                  "source_role", "provenance", "scope", "pinned_by",
                  "created_at", "revoked_at"):
            assert c in cols, f"conversation_pinned_constraints 缺列 {c}"

    def test_deletion_manifests_columns(self):
        cols = DeletionManifest.__table__.columns.keys()
        for c in ("user_id", "request_id", "scope_json", "status",
                  "residual_json", "created_at", "completed_at"):
            assert c in cols, f"deletion_manifests 缺列 {c}"

    def test_dream_repair_reports_columns(self):
        cols = DreamRepairReport.__table__.columns.keys()
        for c in ("user_id", "run_date", "probe_count", "hit_count",
                  "miss_count", "repairs_json", "hit_at_k_before",
                  "hit_at_k_after", "cost_tokens"):
            assert c in cols, f"dream_repair_reports 缺列 {c}"

    def test_step_checkpoints_columns(self):
        cols = StepCheckpoint.__table__.columns.keys()
        for c in ("deathmatch_id", "step_id", "context_cursor", "snapshot_id",
                  "created_at"):
            assert c in cols, f"step_checkpoints 缺列 {c}"

    def test_memory_eval_runs_columns(self):
        cols = MemoryEvalRun.__table__.columns.keys()
        for c in ("run_id", "suite", "arm", "metrics_json", "created_at"):
            assert c in cols, f"memory_eval_runs 缺列 {c}"


class TestColumnChanges:
    def test_memory_concepts_retention_columns(self):
        cols = MemoryConcept.__table__.columns
        assert "retention_score" in cols
        assert "fidelity_state" in cols
        assert cols["fidelity_state"].default.arg == "full"

    def test_agent_dreams_taint_columns(self):
        cols = AgentDream.__table__.columns
        assert "provenance_taint" in cols
        assert "scanned" in cols
        assert cols["scanned"].default.arg is False

    def test_conversation_artifacts_deleted_at_migration(self):
        stmt = _stmts().get("conv_artifacts_deleted_at", "")
        assert "conversation_artifacts" in stmt and "deleted_at" in stmt, (
            "缺 conversation_artifacts.deleted_at 迁移")
        assert "TIMESTAMPTZ" in stmt.upper()


class TestMigrationPlacement:
    """用户权利/基础设施表 → pgvector 标记之前；memory_v2 列变更 → 后缀。
    （pgvector 缺失时后缀整体跳过，遗忘/撤销面不得随之消失。）"""

    def test_infra_tables_before_pgvector_anchor(self):
        names = _names()
        anchor = names.index(m._MEMORY_MIGRATION_START)
        for v in ("cpc_create", "dm_create", "drr_create", "sc_create",
                  "mer_create", "conv_artifacts_deleted_at"):
            assert v in names, f"缺迁移 {v}"
            assert names.index(v) < anchor, f"{v} 必须置于 pgvector_extension 之前"

    def test_memory_v2_columns_after_pgvector_anchor(self):
        names = _names()
        anchor = names.index(m._MEMORY_MIGRATION_START)
        for v in ("mc_retention_score", "mc_fidelity_state",
                  "idx_concepts_user_retention", "ad_provenance_taint",
                  "ad_scanned"):
            assert v in names, f"缺迁移 {v}"
            assert names.index(v) > anchor, f"{v} 必须置于 pgvector 后缀"

    def test_index_statements(self):
        stmts = _stmts()
        assert "revoked_at" in stmts["cpc_idx_conv_revoked"]
        assert "retention_score DESC" in stmts["idx_concepts_user_retention"]

    def test_idempotent_sql(self):
        for v in ("mc_retention_score", "mc_fidelity_state",
                  "ad_provenance_taint", "ad_scanned", "conv_artifacts_deleted_at"):
            assert "ADD COLUMN IF NOT EXISTS" in _stmts()[v], f"{v} 非幂等"


class TestBackfillScript:
    def test_backfill_module_exists_and_dry_run_default(self):
        import importlib

        mod = importlib.import_module("scripts.backfill_retention_score")
        src = inspect.getsource(mod)
        assert "--execute" in src, "backfill 必须以 --execute 显式开启写库"
        assert "IS NULL" in src, "backfill 必须幂等（WHERE ... IS NULL）"
        assert mod.main.__doc__ or "dry-run" in src.lower()

    def test_backfill_score_function_pure(self):
        import importlib

        mod = importlib.import_module("scripts.backfill_retention_score")
        rows = [
            {"id": "a", "weight": 0.9, "recurrence_count": 5,
             "last_recurrence_at": None, "created_at": None,
             "retention_score": None},
            {"id": "b", "weight": 0.1, "recurrence_count": 0,
             "last_recurrence_at": None, "created_at": None,
             "retention_score": 0.7},
        ]
        plan = mod.plan_backfill(rows)
        assert len(plan) == 1, "已有 retention_score 的行必须跳过（幂等）"
        assert plan[0]["retention_score"] > 0

    def test_zero_score_counts_as_unset(self):
        import importlib

        mod = importlib.import_module("scripts.backfill_retention_score")
        rows = [{"id": "c", "weight": 0.5, "recurrence_count": 0,
                 "last_recurrence_at": None, "created_at": None,
                 "retention_score": 0}]
        plan = mod.plan_backfill(rows)
        assert len(plan) == 1, (
            "DEFAULT 0 落下的存量行必须视为待回填——列 DDL 使 IS NULL 永不命中")
