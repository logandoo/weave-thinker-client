"""v1 记忆退休（方案 A）— profile 提炼输入源与回落门控（2026-09-13）。

- `memory_profile_service._load_recent_summaries` 改读 v2 产物：
  memory_episodes.narrative（近 14 天）→ 冷启动兜底 extracted 概念描述；
  不再触碰 agent_memories.daily-summary（v2 下已停摆）。
- `memory_retrieval_service._get_profile_summary` 在 v2 运行时不再回落
  v1 memory_summary。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_memory_profile_source.py -v
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.services.memory_profile_service as profile_svc  # noqa: E402
import app.services.memory_retrieval_service as retrieval_svc  # noqa: E402
import app.services.memory_runtime_state as runtime_state  # noqa: E402


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0][0] if self._rows else None


class _DB:
    def __init__(self, responses):
        self.responses = responses
        self.queries = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.queries.append(sql)
        for key, rows in self.responses:
            if key in sql:
                return _Res(rows)
        return _Res([])


class TestProfileSource:
    def test_episodes_preferred(self):
        db = _DB([("memory_episodes", [("episode narrative",)])])
        out = asyncio.run(profile_svc._load_recent_summaries(db, "u1"))
        assert out == ["episode narrative"]
        assert "memory_episodes" in db.queries[0]
        assert all("agent_memories" not in q for q in db.queries)

    def test_fallback_to_extracted_concepts(self):
        db = _DB([("memory_concepts", [("concept desc",)])])
        out = asyncio.run(profile_svc._load_recent_summaries(db, "u1"))
        assert out == ["concept desc"]
        assert len(db.queries) == 2
        assert "memory_concepts" in db.queries[1]
        assert all("agent_memories" not in q for q in db.queries)

    def test_empty_when_both_empty(self):
        db = _DB([])
        out = asyncio.run(profile_svc._load_recent_summaries(db, "u1"))
        assert out == []


class TestProfileSummaryGate:
    def test_v2_runtime_skips_v1_memory_summary_fallback(self, monkeypatch):
        async def _no_facts(db, user_id, limit=6):
            return []
        monkeypatch.setattr(profile_svc, "get_profile_concepts", _no_facts)
        monkeypatch.setattr(runtime_state, "memory_runtime_enabled", lambda cfg: True)
        db = _DB([("user_agent_states", [("stale v1 summary",)])])
        out = asyncio.run(retrieval_svc._get_profile_summary(db, "u1"))
        assert out is None
        assert all("user_agent_states" not in q for q in db.queries), (
            "v2 运行时仍查询了 v1 memory_summary")

    def test_v1_mode_keeps_legacy_fallback(self, monkeypatch):
        async def _no_facts(db, user_id, limit=6):
            return []

        async def _stable(text, user_id):
            return ["stable profile line"]

        monkeypatch.setattr(profile_svc, "get_profile_concepts", _no_facts)
        monkeypatch.setattr(runtime_state, "memory_runtime_enabled", lambda cfg: False)
        monkeypatch.setattr(retrieval_svc, "_filter_stable_profile_lines", _stable)
        db = _DB([("user_agent_states", [("stale v1 summary",)])])
        out = asyncio.run(retrieval_svc._get_profile_summary(db, "u1"))
        assert out == "stable profile line"
