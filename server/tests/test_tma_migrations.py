"""P1a: taint/verdict tables exist and are not inside the pgvector skip window."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import database as db
from app.db.migrations import STARTUP_MIGRATIONS, _MEMORY_MIGRATION_START


def test_tma_tables_and_models_exist():
    versions = [version for version, _statement in STARTUP_MIGRATIONS]
    for version in (
        "create_taint_states",
        "create_memory_verdicts",
        "create_memory_verdicts_idx",
        "create_memory_verdict_heads",
        "memory_verdicts_tma_mode",
        "memory_verdicts_tma_blocked",
        "memory_verdicts_tma_epoch",
    ):
        assert version in versions, version
    alter_sql = " ".join(
        stmt for version, stmt in STARTUP_MIGRATIONS
        if version.startswith("memory_verdicts_tma_")
    )
    assert "ADD COLUMN IF NOT EXISTS mode" in alter_sql
    assert "ADD COLUMN IF NOT EXISTS blocked" in alter_sql
    assert "ADD COLUMN IF NOT EXISTS created_at_epoch" in alter_sql
    assert db.TaintState.__tablename__ == "taint_states"
    assert db.MemoryVerdict.__tablename__ == "memory_verdicts"
    assert db.MemoryVerdictHead.__tablename__ == "memory_verdict_heads"


def test_tma_migrations_are_before_pgvector_window():
    versions = [version for version, _statement in STARTUP_MIGRATIONS]
    window = versions.index(_MEMORY_MIGRATION_START)
    for version in (
        "create_taint_states",
        "create_memory_verdicts",
        "create_memory_verdicts_idx",
        "create_memory_verdict_heads",
    ):
        assert versions.index(version) < window, f"{version} must run without pgvector"
