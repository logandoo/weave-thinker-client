"""C1 context continuity: anchored rolling summary + resume pack (2026-10-18)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import context_continuity_service as ccs


def _msg(role, content, tool_calls=None):
    return {"role": role, "content": content, "tool_calls": tool_calls}


def test_extract_artifacts_from_tool_args_and_result():
    msgs = [
        _msg("assistant", "", tool_calls=[{
            "function": {"name": "workspace_write",
                         "arguments": '{"path": "src/app.py", "content": "x"}'}
        }]),
        _msg("tool", "saved\nFull output saved to: /tmp/out.txt"),
    ]
    found = ccs.extract_artifacts(msgs)
    paths = [p for p, _k, _s in found]
    assert "src/app.py" in paths
    assert "/tmp/out.txt" in paths


def test_extractive_summary_keeps_paths_not_payload():
    msgs = [
        _msg("user", "请修 src/main.py"),
        _msg("tool", "x" * 5000 + "\nFull output saved to: /tmp/big.txt"),
    ]
    summary = ccs._extractive_summary(msgs)
    assert "src/main.py" in summary
    assert "/tmp/big.txt" in summary
    assert "x" * 100 not in summary


def test_resume_pack_includes_artifacts_and_discipline(monkeypatch):
    async def fake_artifacts(_db, _cid):
        return ["src/main.py", "/tmp/out.txt"]

    monkeypatch.setattr(ccs, "_load_artifacts", fake_artifacts)
    msgs = [_msg("user", "继续修 main.py")]
    pack = asyncio.run(ccs.build_resume_pack(None, "c1", "已完成第一步", msgs))
    assert "src/main.py" in pack
    assert "session_search" in pack
    assert "第一动作" in pack
    assert len(pack) <= ccs.MAX_RESUME_CHARS


def test_truncation_prefix_has_disclosure(monkeypatch):
    async def fake_record(_db, _cid, _msgs):
        return None

    async def fake_summary(_db, _cid, _msgs, llm=None):
        return "摘要：已完成 A，待做 B"

    async def fake_pack(_db, _cid, _summary, _msgs):
        return "<context_continuity>pack</context_continuity>"

    async def fake_execute(*_a, **_k):
        class R:
            def fetchone(self):
                return None
        return R()

    class FakeDB:
        async def execute(self, *_a, **_k):
            return await fake_execute()

    monkeypatch.setattr(ccs, "record_artifacts", fake_record)
    monkeypatch.setattr(ccs, "ensure_rolling_summary", fake_summary)
    monkeypatch.setattr(ccs, "build_resume_pack", fake_pack)
    msgs = [_msg("user", f"消息 {i}") for i in range(10)]
    prefix = asyncio.run(ccs.build_truncation_prefix(FakeDB(), "c1", msgs))
    assert "已省略 10 条" in prefix
    assert "恢复包" in prefix


def test_rolling_summary_appends_only_new_span():
    calls = []

    class Result:
        def __init__(self, row=None):
            self._row = row

        def fetchone(self):
            return self._row

    class FakeDB:
        async def execute(self, sql, params=None):
            calls.append((str(sql), params or {}))
            if "SELECT summary" in str(sql):
                return Result(("旧摘要：已完成 A", 1, "m1"))
            return Result()

    msgs = [
        _msg("user", "消息 1"),
        _msg("assistant", "消息 2"),
        _msg("user", "消息 3"),
    ]
    summary = asyncio.run(ccs.ensure_rolling_summary(FakeDB(), "c1", msgs))
    assert "旧摘要" in summary
    assert "消息 2" in summary and "消息 3" in summary
    insert = [p for s, p in calls if "INSERT INTO conversation_rolling_summaries" in s]
    assert insert and insert[-1]["n"] == 3
