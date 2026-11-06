"""P1 S3: resume pack carries REMORY residual state, not only prose summary."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.context_continuity_service import format_resume_state


def test_resume_state_contains_plan_settled_retracted_and_acceptance():
    text = format_resume_state(
        {"steps": [{"id": "s1", "status": "done", "description": "write report"}],
         "acceptance_criteria": ["file exists"]},
        [{"summary": "decided A"}],
        [{"claim": "false lead"}],
        {"status": "partial"},
    )
    assert "计划状态" in text and "s1" in text
    assert "验收标准" in text and "file exists" in text
    assert "已定案" in text and "decided A" in text
    assert "已撤回" in text and "false lead" in text
    assert "status=partial" in text


def test_resume_state_is_empty_for_no_state():
    assert format_resume_state(None, None, None) == ""


class _Result:
    def __init__(self, rows=None, row=None):
        self._rows = rows or []
        self._row = row

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


class _DB:
    async def execute(self, statement, params=None):
        sql = str(statement)
        if "conversation_artifacts" in sql:
            return _Result(rows=[("report.md",)])
        if "FROM conversations" in sql:
            return _Result(row=(
                {"steps": [{"id": "s1", "status": "done", "description": "write"}]},
                [{"summary": "settled"}],
                [{"claim": "retracted"}],
                {"status": "complete"},
            ))
        return _Result()


def test_build_resume_pack_contains_remory_tail_and_closed_provenance():
    import asyncio
    from app.services.context_continuity_service import build_resume_pack

    pack = asyncio.run(build_resume_pack(_DB(), "c1", "summary", [
        {"role": "user", "content": "old ask"},
    ]))
    assert "<provenance " in pack
    assert pack.count("</provenance>") == 1
    assert "计划状态" in pack
    assert "已撤回" in pack
    assert "report.md" in pack
