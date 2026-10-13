"""TDD tests: file 记忆↔概念链接（WFM 2609.18182 残余）——
检索侧拓扑扩展：①unit→concept 链接召回（subconscious 命中沿 source_unit_ids
并入关联概念）②concept→unit 稠密上下文（注入概念行携带源条目摘录）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_memory_concept_link_expansion.py -v
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.memory_retrieval_service as mrs
from app.services.memory_retrieval_service import RetrievalCandidate


def _run(coro):
    return asyncio.run(coro)


class FakeResult:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return [r[0] for r in self._rows]


class FakeDB:
    """Ordered fake session: execute() pops the next prepared result."""

    def __init__(self, results=None, fail=False):
        self._results = list(results or [])
        self.fail = fail
        self.stmts = []

    async def execute(self, stmt, *a, **k):
        self.stmts.append(str(stmt))
        if self.fail:
            raise RuntimeError("db down")
        return FakeResult(self._results.pop(0) if self._results else [])


def _sub(id_, score=0.8):
    return RetrievalCandidate(
        id=id_, tier="subconscious", score=score, content="原文",
        metadata={"raw_text": "原文", "created_at": "2026-09-25"},
    )


def _link_row(cid, name, unit_ids):
    return (
        cid, name, f"{name}的简述", f"{name}的详情", 0.6, 0.5,
        "user_stated", "semantic", "[]", None, 14.0,
        "2026-09-25", json.dumps(unit_ids),
    )


# ── unit→concept 链接召回扩展 ─────────────────────────────────────────────

def test_link_expansion_adds_missing_concepts():
    db = FakeDB(results=[[
        _link_row("c-linked", "关联概念", ["u1"]),
    ]])
    existing = [RetrievalCandidate(
        id="c-seed", tier="concept", score=0.9, content="种子",
        metadata={"canonical_name": "种子概念", "weight": 0.5},
    )]
    out = _run(mrs._stage2b_link_expansion(
        db, "user-1", existing, [_sub("u1")], [], enabled=True))
    ids = [c.id for c in out]
    assert "c-linked" in ids, "命中 unit 的关联概念必须并入候选（file_link_expansion）"
    added = [c for c in out if c.id == "c-linked"][0]
    assert added.metadata.get("source") == "file_link_expansion"
    assert added.tier == "concept"
    assert "关联概念" in added.metadata.get("canonical_name", "")


def test_link_expansion_skips_existing_and_bounds():
    rows = [_link_row(f"c{i}", f"概念{i}", ["u1"]) for i in range(6)]
    rows.append(_link_row("c-seed", "种子概念", ["u1"]))
    db = FakeDB(results=[rows])
    existing = [RetrievalCandidate(
        id="c-seed", tier="concept", score=0.9, content="种子",
        metadata={"canonical_name": "种子概念"},
    )]
    out = _run(mrs._stage2b_link_expansion(db, "user-1", existing, [_sub("u1")], [], enabled=True))
    ids = [c.id for c in out]
    assert ids.count("c-seed") == 1, "已存在的概念不得重复并入"
    added = [i for i in ids if i.startswith("c") and i != "c-seed"]
    assert len(added) <= 3, "链接扩展必须有界（默认每轮≤3）"


def test_link_expansion_disabled_by_config():
    """开关路径：显式 False + 配置默认路径（enabled=None 读 config）。"""
    db = FakeDB(results=[[_link_row("c-linked", "关联概念", ["u1"])]])
    out = _run(mrs._stage2b_link_expansion(
        db, "user-1", [], [_sub("u1")], [], enabled=False))
    assert out == [], "开关关闭时不得扩展"
    db2 = FakeDB(results=[[_link_row("c2", "概念2", ["u1"])]])
    mrs.config.memory_retrieval["concept_link_expansion_enabled"] = False
    try:
        out2 = _run(mrs._stage2b_link_expansion(db2, "user-1", [], [_sub("u1")], []))
        assert out2 == [], "enabled=None 且配置关闭时不得扩展"
    finally:
        mrs.config.memory_retrieval.pop("concept_link_expansion_enabled", None)
    db3 = FakeDB(results=[[_link_row("c3", "概念3", ["u1"])]])
    mrs.config.memory_retrieval["concept_link_expansion_enabled"] = True
    try:
        out3 = _run(mrs._stage2b_link_expansion(db3, "user-1", [], [_sub("u1")], []))
        assert [c.id for c in out3] == ["c3"], "enabled=None 且配置开启时必须扩展"
    finally:
        mrs.config.memory_retrieval.pop("concept_link_expansion_enabled", None)


def test_link_expansion_fail_open():
    db = FakeDB(fail=True)
    seed = [_sub("u1")]
    out = _run(mrs._stage2b_link_expansion(db, "user-1", [], seed, [], enabled=True))
    assert out == [], "DB 失败 fail-open：返回原候选，不抛出"


# ── concept→unit 稠密上下文（源条目摘录）─────────────────────────────────

def test_fetch_linked_unit_excerpts_maps_concept_to_excerpt():
    db = FakeDB(results=[
        [("c1", json.dumps(["u9"]))],          # concept → source_unit_ids
        [("u9", "文件记忆原文：用户偏好深色主题的详细记录")],  # unit 文本
    ])
    got = _run(mrs._fetch_linked_unit_excerpts(db, "user-1", ["c1"]))
    assert "c1" in got
    assert "深色主题" in got["c1"], "概念必须取回其源条目摘录"


def test_concept_line_renders_source_excerpt():
    line = mrs._concept_line_with_link(
        "— 主题偏好：深色主题 [权重: 0.60]",
        "文件记忆原文：用户偏好深色主题的详细记录",
    )
    assert "— 主题偏好：深色主题" in line
    assert "源条目" in line and "深色主题" in line
    # 无摘录 → 原行不变
    assert mrs._concept_line_with_link("原行", "") == "原行"


def test_excerpt_is_clipped_and_flat():
    db = FakeDB(results=[
        [("c1", json.dumps(["u9"]))],
        [("u9", "X" * 500 + "\n第二行")],
    ])
    got = _run(mrs._fetch_linked_unit_excerpts(db, "user-1", ["c1"]))
    assert len(got["c1"]) < 200, "摘录必须有界"
    assert "\n" not in got["c1"], "摘录必须单行化"


# ── A4.9 复审修复覆盖（r1 Important/Missing pins）─────────────────────────

def test_link_candidate_gate_requires_confirmation():
    """I1/I3：链接候选未经 embedding/CE 确认不得过门槛（同 cluster/relation）。"""
    unconfirmed = RetrievalCandidate(
        id="c-linked", tier="concept", score=0.45, content="x",
        metadata={"source": "file_link_expansion"},
    )
    assert mrs._cand_gate_score(unconfirmed) == 0.0, "无确认的链接候选必须被门槛拒绝"
    confirmed = RetrievalCandidate(
        id="c-linked", tier="concept", score=0.45, content="x",
        metadata={"source": "file_link_expansion", "embedding_sim": 0.72},
    )
    assert mrs._cand_gate_score(confirmed) == 0.72, "embedding 确认后按 sim 通过"


def test_link_query_guards_retired_and_is_deterministic():
    """I2/M2：SQL 必须带 valid_to IS NULL（已失效/被合并概念不复活）+ ORDER BY。"""
    db = FakeDB(results=[[]])
    _run(mrs._stage2b_link_expansion(db, "user-1", [], [_sub("u1")], [], enabled=True))
    assert db.stmts, "必须真的发出查询"
    sql = db.stmts[0]
    assert "valid_to IS NULL" in sql
    assert "ORDER BY" in sql
    assert "user_id = :u" in sql


def test_link_expansion_uses_episodic_seeds():
    """M1：episodic 候选同为 source_unit_ids 种子（episodes id 同链接）。"""
    db = FakeDB(results=[[_link_row("c-epi-linked", "事件关联概念", ["e1"])]])
    epi = RetrievalCandidate(
        id="e1", tier="episodic", score=0.9, content="事件",
        metadata={"narrative": "事件"},
    )
    out = _run(mrs._stage2b_link_expansion(
        db, "user-1", [], [], [epi], enabled=True))
    assert [c.id for c in out] == ["c-epi-linked"]


def test_link_expansion_returns_new_list_not_mutating():
    seed_list = [RetrievalCandidate(
        id="c-seed", tier="concept", score=0.9, content="s",
        metadata={"canonical_name": "种子"})]
    db = FakeDB(results=[[_link_row("c-new", "新概念", ["u1"])]])
    out = _run(mrs._stage2b_link_expansion(
        db, "user-1", seed_list, [_sub("u1")], [], enabled=True))
    assert len(seed_list) == 1, "不得原地改写调用方入参"
    assert len(out) == 2


def test_excerpt_json_non_list_guard():
    db = FakeDB(results=[
        [("c1", '{"not": "a list"}')],
        [],
    ])
    got = _run(mrs._fetch_linked_unit_excerpts(db, "user-1", ["c1"]))
    assert got == {}, "source_unit_ids 非列表值必须安全跳过"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
