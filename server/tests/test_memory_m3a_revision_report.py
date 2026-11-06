"""TDD — W3 M3a 离线修订候选报告（ReHarness 2610.12668 backward revision，先离线）。

技术规格源：docs/AUDIT_2026-10-13_deathmatch_memory_papers.md §3.3 改进设计（M3a）
+ §10.1 勘正 A7（join 以**注入集**为准——recall log `candidate_ids` 口径钉死）
+ §10.4 E4（离线纯读，不连生产写、不自动改）。

join：memory_recall_log（注入了什么，candidate_ids=注入集）× adoption
（memory_adoption_service 口径）× clarifications ×（可选）死磕步骤结局 →
「疑似误导/过度泛化」候选清单（含源单元引用）→ docs/ markdown 供人工裁定
（edit/merge/forget）——纯读离线，不自动改。

纯函数 `compute_revision_candidates(rows)` / `render_revision_report(...)` 落
`scripts/memory_eval_report.py`（revision 子命令）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_memory_m3a_revision_report.py -v
"""
import asyncio
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.memory_eval_report import (
    compute_revision_candidates,
    normalize_adoption_rows,
    normalize_clarification_rows,
    normalize_deathmatch_rows,
    normalize_recall_rows,
    render_revision_report,
)


def _recall(rid, uid, conv, qh, injected, **kw):
    row = {"id": rid, "user_id": uid, "conversation_id": conv, "query_hash": qh,
           "candidate_ids": injected, "created_at": kw.pop("created_at", None)}
    row.update(kw)
    return row


# ── A7 口径：join 以注入集为准 ─────────────────────────────────────────────

def test_a7_join_uses_injection_set_not_pool():
    """candidate_ids=注入集（A7 勘正）：台账里的全候选池字段不得被 join 采用。"""
    rows = {
        "recall": [
            _recall("r1", "u1", "conv1", "q1", ["inj-1"],
                    all_candidates=["inj-1", "pool-9"]),
        ],
    }
    out = compute_revision_candidates(rows)
    ids = {c["unit_id"] for c in out} if out else set()
    assert "pool-9" not in ids, "全候选池 id 不得进入 join（A7：以注入集为准）"
    # 注入集 id 按规则命中（inj-1 仅注入 1 轮无澄清 → 不成候选，也不得报 pool-9）
    assert "inj-1" not in ids


def test_a7_candidate_ids_json_string_normalized():
    raw = [_recall("r1", "u1", "conv1", "q1", json.dumps(["u1", "u2"]))]
    norm = normalize_recall_rows(raw)
    assert norm[0]["candidate_ids"] == ["u1", "u2"]
    assert normalize_recall_rows([_recall("r2", "u1", "c", "q", None)])[0]["candidate_ids"] == []


# ── R1 疑似误导：被澄清修正的单元 ─────────────────────────────────────────

def test_clarification_rule_flags_unit_as_misleading():
    rows = {
        "recall": [
            _recall("r1", "u1", "conv1", "q1", ["c-1"]),
            _recall("r2", "u1", "conv1", "q2", ["c-1"]),
        ],
        "clarifications": [
            {"id": "cl1", "user_id": "u1", "affected_concept_ids": ["c-1"],
             "correction_type": "negate", "created_at": None},
        ],
    }
    out = compute_revision_candidates(rows)
    assert len(out) == 1
    cand = out[0]
    assert cand["unit_id"] == "c-1"
    assert "clarified" in {r["code"] for r in cand["reasons"]}
    assert "疑似误导" in cand["labels"]
    assert "cl1" in cand["source_refs"]["clarification_ids"]
    assert "r1" in cand["source_refs"]["recall_ids"]
    assert "forget" in cand["verdicts"]


def test_clarification_affected_ids_json_normalized():
    raw = [{"id": "cl1", "user_id": "u1", "affected_concept_ids": json.dumps(["c-7"]),
            "correction_type": "refine", "created_at": None}]
    assert normalize_clarification_rows(raw)[0]["affected_concept_ids"] == ["c-7"]


# ── R2 疑似误导：高频注入零采纳 ────────────────────────────────────────────

def test_never_adopted_over_injection_rule():
    rows = {
        "recall": [
            _recall(f"r{i}", "u1", f"conv{i%2+1}", f"q{i}", ["c-2"]) for i in range(3)
        ],
    }
    out = compute_revision_candidates(rows)
    cand = out[0]
    assert cand["unit_id"] == "c-2"
    assert "never_adopted" in {r["code"] for r in cand["reasons"]}
    assert "疑似误导" in cand["labels"]
    assert cand["injected_turns"] == 3 and cand["adoption_count"] == 0


def test_single_injection_without_adoption_not_flagged():
    rows = {"recall": [_recall("r1", "u1", "conv1", "q1", ["c-2"])]}
    assert compute_revision_candidates(rows) == []


def test_adoption_suppresses_never_adopted_rule():
    rows = {
        "recall": [
            _recall(f"r{i}", "u1", "conv1", f"q{i}", ["c-2"]) for i in range(3)
        ],
        "adoption": [{"unit_id": "c-2", "adopted": True, "created_at": None}],
    }
    out = compute_revision_candidates(rows)
    assert out == [], "被采纳过的单元不得再按零采纳规则报候选"


# ── R3 过度泛化：跨多查询/多会话注入、低采纳 ───────────────────────────────

def test_over_generalization_rule():
    rows = {
        "recall": [
            _recall("r1", "u1", "conv1", "q1", ["c-3"]),
            _recall("r2", "u1", "conv2", "q2", ["c-3"]),
            _recall("r3", "u1", "conv3", "q3", ["c-3"]),
        ],
    }
    out = compute_revision_candidates(rows)
    cand = out[0]
    assert "over_generalized" in {r["code"] for r in cand["reasons"]}
    assert "过度泛化" in cand["labels"]
    assert "merge" in cand["verdicts"]
    assert cand["distinct_queries"] == 3 and cand["distinct_conversations"] == 3


def test_over_generalization_requires_cross_conversation():
    rows = {
        "recall": [
            _recall("r1", "u1", "conv1", "q1", ["c-3"]),
            _recall("r2", "u1", "conv1", "q2", ["c-3"]),
            _recall("r3", "u1", "conv1", "q3", ["c-3"]),
        ],
    }
    out = compute_revision_candidates(rows)
    codes = {r["code"] for c in out for r in c["reasons"]}
    assert "over_generalized" not in codes, "单会话内多查询不算过度泛化"


# ── R4 可选：死磕步骤结局关联 ──────────────────────────────────────────────

def test_deathmatch_failure_rule_optional_dataset():
    rows = {
        "recall": [
            _recall("r1", "u1", "dm-conv", "q1", ["c-4"]),
            _recall("r2", "u1", "dm-conv", "q2", ["c-4"]),
        ],
        "deathmatch_steps": [
            {"step_id": "s1", "conversation_id": "dm-conv", "outcome": "failed",
             "failure_signature": "goal_unmet", "unit_ids": ["c-4"], "created_at": None},
            {"step_id": "s2", "conversation_id": "dm-conv", "outcome": "failed",
             "failure_signature": "goal_unmet", "unit_ids": ["c-4"], "created_at": None},
        ],
    }
    out = compute_revision_candidates(rows)
    cand = out[0]
    assert "deathmatch_failure" in {r["code"] for r in cand["reasons"]}
    assert set(cand["source_refs"]["deathmatch_step_ids"]) == {"s1", "s2"}
    assert "疑似误导" in cand["labels"]


def test_deathmatch_ok_steps_do_not_flag():
    rows = {
        "recall": [_recall("r1", "u1", "dm-conv", "q1", ["c-4"])],
        "deathmatch_steps": [
            {"step_id": "s1", "conversation_id": "dm-conv", "outcome": "ok",
             "unit_ids": ["c-4"], "created_at": None},
        ],
    }
    assert compute_revision_candidates(rows) == []


def test_deathmatch_rows_json_unit_ids_normalized():
    raw = [{"step_id": "s1", "conversation_id": "dm-conv", "outcome": "failed",
            "unit_ids": json.dumps(["c-4"]), "created_at": None}]
    assert normalize_deathmatch_rows(raw)[0]["unit_ids"] == ["c-4"]


# ── 排序/确定性/渲染 ──────────────────────────────────────────────────────

def test_compute_deterministic_and_sorted_by_score():
    rows = {
        "recall": (
            [_recall(f"r{i}", "u1", f"conv{i%2+1}", f"q{i}", ["c-9"]) for i in range(3)]
            + [_recall("ra", "u1", "conv1", "qa", ["c-1"])]
        ),
        "clarifications": [
            {"id": "cl1", "user_id": "u1", "affected_concept_ids": ["c-1"],
             "correction_type": "negate", "created_at": None},
        ],
    }
    a = compute_revision_candidates(rows)
    b = compute_revision_candidates(rows)
    assert a == b, "纯函数输出必须确定性"
    assert {c["unit_id"] for c in a} == {"c-1", "c-9"}
    assert all(a[i]["score"] >= a[i + 1]["score"] for i in range(len(a) - 1))
    # 分数=触发规则权重和：单条澄清修正 vs 零采纳+过度泛化双规则
    by_id = {c["unit_id"]: c for c in a}
    assert {r["code"] for r in by_id["c-1"]["reasons"]} == {"clarified"}
    assert by_id["c-9"]["score"] > by_id["c-1"]["score"]


def test_render_report_contains_source_refs_and_verdicts():
    rows = {
        "recall": [
            _recall(f"r{i}", "u1", f"conv{i%2+1}", f"q{i}", ["c-5"]) for i in range(3)
        ],
        "units": [{"unit_id": "c-5", "name": "部署端口", "tier": "concept"}],
    }
    cands = compute_revision_candidates(rows)
    md = render_revision_report(cands, {"days": 30})
    assert "记忆修订候选报告" in md
    assert "c-5" in md, "源单元引用必须包含单元 id"
    assert "部署端口" in md
    assert "疑似误导" in md or "过度泛化" in md
    assert "edit" in md and "merge" in md and "forget" in md, "人工裁定词表必含 edit/merge/forget"
    assert "不自动" in md or "纯读" in md, "必须声明不自动修改记忆"


def test_render_empty_candidates_is_valid_markdown():
    md = render_revision_report([], {"days": 7})
    assert "记忆修订候选报告" in md
    assert "无候选" in md


# ── sqlite/内存假行喂 join 逻辑 ────────────────────────────────────────────

def _seed_sqlite(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE recall_log (id TEXT, user_id TEXT, conversation_id TEXT,
            query_hash TEXT, candidate_ids TEXT, created_at TEXT);
        CREATE TABLE adoption (unit_id TEXT, adopted INTEGER, created_at TEXT);
        CREATE TABLE clarifications (id TEXT, user_id TEXT,
            affected_concept_ids TEXT, correction_type TEXT, created_at TEXT);
        CREATE TABLE deathmatch_steps (step_id TEXT, conversation_id TEXT,
            outcome TEXT, failure_signature TEXT, unit_ids TEXT, created_at TEXT);
    """)
    for i in range(3):
        conn.execute(
            "INSERT INTO recall_log VALUES (?,?,?,?,?,?)",
            (f"r{i}", "u1", f"conv{i%2+1}", f"q{i}", json.dumps(["c-6"]), None))
    conn.execute(
        "INSERT INTO clarifications VALUES (?,?,?,?,?)",
        ("cl9", "u1", json.dumps(["c-6"]), "negate", None))
    conn.commit()


def test_sqlite_memory_rows_feed_join():
    """内存 sqlite 假行 → normalize → compute（join 逻辑可离线喂任意行源）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _seed_sqlite(conn)
        recall = normalize_recall_rows(
            [dict(r) for r in conn.execute("SELECT * FROM recall_log")])
        clar = normalize_clarification_rows(
            [dict(r) for r in conn.execute("SELECT * FROM clarifications")])
        adoption = normalize_adoption_rows(
            [dict(r) for r in conn.execute("SELECT * FROM adoption")])
        dm = normalize_deathmatch_rows(
            [dict(r) for r in conn.execute("SELECT * FROM deathmatch_steps")])
    finally:
        conn.close()
    out = compute_revision_candidates({
        "recall": recall, "clarifications": clar,
        "adoption": adoption, "deathmatch_steps": dm,
    })
    assert len(out) == 1
    cand = out[0]
    assert cand["unit_id"] == "c-6"
    codes = {r["code"] for r in cand["reasons"]}
    assert "clarified" in codes and "never_adopted" in codes
    assert cand["source_refs"]["clarification_ids"] == ["cl9"]


# ── CLI 子命令缝（revision：--from-json 离线喂入，不连库） ─────────────────

def test_cli_revision_from_json_writes_markdown(tmp_path):
    from scripts.memory_eval_report import main as report_main
    rows = {
        "recall": [
            _recall(f"r{i}", "u1", f"conv{i%2+1}", f"q{i}", ["c-8"]) for i in range(3)
        ],
    }
    src = tmp_path / "rows.json"
    src.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    out_md = tmp_path / "revision.md"
    rc = report_main(["revision", "--from-json", str(src), "--out", str(out_md)])
    assert rc == 0
    text = out_md.read_text(encoding="utf-8")
    assert "记忆修订候选报告" in text and "c-8" in text


# ── E4：DB 模式 --user-id 过滤（fix-round Minors） ─────────────────────────

def test_cli_revision_db_mode_accepts_user_id_filter(monkeypatch):
    """--user-id 必须穿到 _load_rows_db（DB 模式按用户过滤）。"""
    import scripts.memory_eval_report as mer
    captured = {}

    async def fake_load(days, user_id=None):
        captured["days"] = days
        captured["user_id"] = user_id
        return {"recall": [], "clarifications": [], "deathmatch_steps": [],
                "adoption": [], "units": []}

    monkeypatch.setattr(mer, "_load_rows_db", fake_load)
    rc = mer.main(["revision", "--days", "7", "--user-id", "u-42"])
    assert rc == 0
    assert captured["user_id"] == "u-42", "DB 模式必须把 --user-id 传给装载器"


def test_load_rows_db_filters_by_user_id(monkeypatch):
    """真实 SQL 面：--user-id 给定时 recall/clarifications 查询必须带用户过滤。"""
    import scripts.memory_eval_report as mer

    seen = []

    class _Row:
        def __init__(self, mapping):
            self._mapping = mapping

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def __iter__(self):
            return iter(self._rows)

    class _Session:
        async def execute(self, stmt, params=None):
            seen.append((str(stmt), dict(params or {})))
            return _Result([])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Maker:
        def __call__(self):
            return _Session()

    class _Engine:
        async def dispose(self):
            return None

    import sqlalchemy.ext.asyncio as sa_async
    monkeypatch.setattr(sa_async, "create_async_engine", lambda url: _Engine())
    monkeypatch.setattr(sa_async, "async_sessionmaker", lambda engine, **k: _Maker())

    asyncio.run(mer._load_rows_db(7, user_id="u-42"))
    assert seen, "必须发出查询"
    user_scoped = [s for s, _p in seen if "memory_recall_log" in s or "memory_clarifications" in s]
    assert user_scoped, "recall/clarifications 查询必须在场"
    for stmt, params in seen:
        if "memory_recall_log" in stmt or "memory_clarifications" in stmt:
            assert "user_id" in stmt and params.get("uid") == "u-42", \
                f"用户过滤必须进 SQL 与绑定参数: {stmt!r} {params!r}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
