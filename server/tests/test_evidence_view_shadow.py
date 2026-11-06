"""W2 续波 A1/A2（记忆/长程 v2，2026-10-23，判据 #48-#53）：

证据视图裁决 + 隐式冲突的 **shadow 运行时接线**——`_build_injection_context_ex`
注入装配处消费 `build_evidence_units`/`adjudicate_conflicts`/`detect_implicit_conflict`，
稀有事件落 `memory_eval_runs`（suite=evidence_view_shadow / implicit_conflict_shadow），
**只记录不改注入集合**（shadow；A/B 后另波换注入集）。

落档仅元数据（unit_id/validity/conflict_group/kind/计数），不含事实正文（D6 口径）。
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.memory_retrieval_service as mrs  # noqa: E402
from app.services.memory_retrieval_service import (  # noqa: E402
    RetrievalCandidate,
    Stage0Result,
)


def _evs():
    """惰性导入：RED 基线（旧代码无 shadow_report/record_evidence_shadow）时
    逐测试失败而非整文件收集失败（对齐 test_memory_batch_cdef_fixes._rls）。"""
    import app.services.memory_evidence_view_service as evs
    return evs


def shadow_report(candidates):
    return _evs().shadow_report(candidates)


def build_evidence_units(candidates):
    return _evs().build_evidence_units(candidates)


class _Res:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._scalar


class _DB:
    """对齐 test_memory_batch_cdef_fixes._DB 的最小 FakeDB 接缝。"""

    def __init__(self, rows=None, scalar=None):
        self.sqls = []
        self._rows = rows or []
        self._scalar = scalar

    async def execute(self, stmt, params=None):
        self.sqls.append(str(stmt))
        return _Res(rows=self._rows, scalar=self._scalar)

    def add(self, obj):
        pass

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _conflict_cands():
    """同 conflict_group、不同 event_time/事实 → 裁决出 supersede + 冲突。"""
    return [
        RetrievalCandidate(id="a", tier="concept", content="站会在 09:30",
                           score=0.9,
                           metadata={"embedding_sim": 0.9, "canonical_name": "站会",
                                     "created_at": "2026-10-01",
                                     "valid_from": "2026-10-01"}),
        RetrievalCandidate(id="b", tier="concept", content="站会改在 10:15",
                           score=0.85,
                           metadata={"embedding_sim": 0.85, "canonical_name": "站会",
                                     "created_at": "2026-10-05",
                                     "valid_from": "2026-10-05"}),
    ]


def _boring_cands():
    return [
        RetrievalCandidate(id="x", tier="concept", content="用户喜欢深色主题",
                           score=0.9,
                           metadata={"embedding_sim": 0.9, "canonical_name": "主题",
                                     "created_at": "2026-09-01",
                                     "valid_from": "2026-09-01"}),
    ]


# ---------------- A1/A2：shadow_report 纯函数 ----------------

def test_shadow_report_none_when_nothing_to_report():
    """稀有事件语义：无 supersede/stale/expired 且无冲突 → None（不写行）。"""
    assert shadow_report([{"id": "x", "content": "用户喜欢深色主题",
                           "valid_from": "2026-09-01", "created_at": "2026-09-01"}]) is None
    assert shadow_report([]) is None


def test_shadow_report_supersede_and_conflict():
    cands = [{"id": c.id, "content": c.content, **c.metadata} for c in _conflict_cands()]
    rep = shadow_report(cands)
    assert rep is not None
    assert rep["unit_count"] == 2
    assert rep["superseded_ids"] == ["a"], "旧者标 superseded"
    assert rep["stale_ids"] == [] and rep["expired_ids"] == []
    kinds = {c["kind"] for c in rep["conflicts"]}
    assert kinds <= {"stale_state", "premise_resistance", "policy_adaptation"}
    assert kinds, "同组异时异事实必出冲突"
    assert all(set(c) == {"group", "kind", "unit_ids"} for c in rep["conflicts"])
    assert all(c["unit_ids"] for c in rep["conflicts"])


def test_shadow_report_metadata_only_no_fact_text():
    """D6 口径：报告不含事实正文/时间点文本。"""
    cands = [{"id": c.id, "content": c.content, **c.metadata} for c in _conflict_cands()]
    dump = json.dumps(shadow_report(cands), ensure_ascii=False)
    for leak in ("站会", "09:30", "10:15"):
        assert leak not in dump, f"事实正文泄漏: {leak}"


def test_shadow_report_deterministic():
    cands = [{"id": c.id, "content": c.content, **c.metadata} for c in _conflict_cands()]
    assert shadow_report(cands) == shadow_report(cands)


# ---------------- 落档（memory_eval_runs，PG） ----------------

def test_record_evidence_shadow_pg():
    dsn = os.environ.get("WT_TEST_PG_URL", "").strip()
    try:
        from app.core.config import get_config
        dsn = dsn or get_config().database_url
    except Exception:
        pass
    if not dsn:
        pytest.skip("no PG DSN")
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.database import Base, MemoryEvalRun
    from app.services.memory_evidence_view_service import record_evidence_shadow

    async def _run():
        eng = create_async_engine(dsn)
        try:
            async with eng.begin() as conn:
                await conn.run_sync(lambda c: Base.metadata.create_all(
                    c, tables=[Base.metadata.tables["memory_eval_runs"]],
                    checkfirst=True))
        except Exception as exc:
            pytest.skip(f"PG unavailable: {type(exc).__name__}")
        Session = async_sessionmaker(eng, expire_on_commit=False)
        cands = [{"id": c.id, "content": c.content, **c.metadata}
                 for c in _conflict_cands()]
        rep = shadow_report(cands)
        rep["llm_fallback"] = False
        async with Session() as db:
            before = set((await db.execute(
                select(MemoryEvalRun.id))).scalars().all())
        written = await record_evidence_shadow(rep, session_factory=Session)
        assert written == 2, "裁决行 + 冲突行各一"
        async with Session() as db:
            rows = (await db.execute(select(MemoryEvalRun).where(
                MemoryEvalRun.run_id.like("evshadow-%")
                | MemoryEvalRun.run_id.like("conflictshadow-%")))).scalars().all()
            mine = [r for r in rows if r.id not in before]
            assert len(mine) == 2
            suites = {r.suite for r in mine}
            assert "evidence_view_shadow" in suites
            assert "implicit_conflict_shadow" in suites
            for r in mine:
                assert r.arm == "shadow"
                dump = json.dumps(r.metrics_json, ensure_ascii=False)
                for leak in ("站会", "09:30", "10:15"):
                    assert leak not in dump, f"落档泄漏事实正文: {leak}"
            # 只删本测试新增行（B 评审 Important：禁止全前缀清理——
            # 运行时 shadow 行同前缀，跨天/跨用户清理=真实遥测数据丢失）
            await db.execute(delete(MemoryEvalRun).where(
                MemoryEvalRun.id.in_([r.id for r in mine])))
            await db.commit()
        await eng.dispose()

    asyncio.run(_run())


def test_record_evidence_shadow_no_event_writes_nothing():
    from app.services.memory_evidence_view_service import record_evidence_shadow

    async def _run():
        assert await record_evidence_shadow(None) == 0
        assert await record_evidence_shadow({
            "unit_count": 1, "validity_counts": {"valid": 1},
            "superseded_ids": [], "stale_ids": [], "expired_ids": [],
            "conflicts": [], "llm_fallback": False}) == 0
    asyncio.run(_run())


# ---------------- 接线（_build_injection_context_ex） ----------------

def _run_ctx(cands, monkeypatch, captured):
    def _cap(report):
        captured.append(report)

    import app.services.memory_evidence_view_service as evs
    monkeypatch.setattr(evs, "record_evidence_shadow_bg", _cap)

    async def _run():
        return await mrs._build_injection_context_ex(
            _DB(rows=[]), "u-shadow", cands, Stage0Result(keywords=["kw"]),
            query_text="kw")
    return asyncio.run(_run())


def test_wiring_emits_shadow_report(monkeypatch):
    captured = []
    ctx, _meta = _run_ctx(_conflict_cands(), monkeypatch, captured)
    assert "[相关概念 Concept]" in ctx, "前置：候选确实被注入"
    assert len(captured) == 1, "装配处消费证据视图（shadow 落档触发）"
    rep = captured[0]
    assert rep["superseded_ids"] == ["a"]
    assert rep["llm_fallback"] is False, "默认纯规则（零 LLM 调用）"
    dump = json.dumps(rep, ensure_ascii=False)
    assert "站会" not in dump


def test_wiring_injection_text_unchanged(monkeypatch):
    """shadow 不改注入集合：开/关两跑注入文本逐字节一致（判据 #49）。"""
    captured = []
    ctx_on, meta_on = _run_ctx(_conflict_cands(), monkeypatch, captured)
    assert captured, "开态应落档"
    mem = mrs.config.memory
    saved = mem.get("evidence_view_shadow_enabled")
    try:
        mem["evidence_view_shadow_enabled"] = False
        captured2 = []
        ctx_off, meta_off = _run_ctx(_conflict_cands(), monkeypatch, captured2)
    finally:
        if saved is None:
            mem.pop("evidence_view_shadow_enabled", None)
        else:
            mem["evidence_view_shadow_enabled"] = saved
    assert captured2 == [], "关态零调用零落档"
    assert ctx_on == ctx_off
    assert meta_on == meta_off


def test_wiring_fail_open(monkeypatch):
    import app.services.memory_evidence_view_service as evs

    def _boom(report):
        raise RuntimeError("shadow write must never break retrieval")

    monkeypatch.setattr(evs, "record_evidence_shadow_bg", _boom)
    ctx, _meta = asyncio.run(mrs._build_injection_context_ex(
        _DB(rows=[]), "u-shadow", _conflict_cands(),
        Stage0Result(keywords=["kw"]), query_text="kw"))
    assert "[相关概念 Concept]" in ctx


def test_llm_fallback_key_reserved_default_false():
    from app.core.config import get_config

    cfg = get_config()
    saved_llm = cfg.memory.get("implicit_conflict_llm_fallback", "unset")
    saved_shadow = cfg.memory.get("evidence_view_shadow_enabled", "unset")
    try:
        cfg.memory.pop("implicit_conflict_llm_fallback", None)
        assert cfg.memory_implicit_conflict_llm_fallback is False
        cfg.memory["implicit_conflict_llm_fallback"] = True
        assert cfg.memory_implicit_conflict_llm_fallback is True
        # fail-safe 布尔解析（B 评审 Minor）：字符串误拼不得把开关翻转
        cfg.memory["implicit_conflict_llm_fallback"] = "false"
        assert cfg.memory_implicit_conflict_llm_fallback is False
        cfg.memory["evidence_view_shadow_enabled"] = "false"
        assert cfg.memory_evidence_view_shadow_enabled is False
    finally:
        for key, saved in (("implicit_conflict_llm_fallback", saved_llm),
                           ("evidence_view_shadow_enabled", saved_shadow)):
            if saved == "unset":
                cfg.memory.pop(key, None)
            else:
                cfg.memory[key] = saved


def test_record_evidence_shadow_row_mapping_hermetic():
    """suite/arm/run_id/metrics 映射 + 稀有事件分档——零 PG 钉死
    （A 评审 Important：PG 测试自跳过即 CI 无覆盖）。"""
    from app.services.memory_evidence_view_service import record_evidence_shadow

    added = []

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def add(self, obj):
            added.append(obj)

        async def commit(self):
            pass

    def _factory():
        return _Sess()

    async def _run():
        # 全事件报告 → 两行（裁决行 + 冲突行）
        assert await record_evidence_shadow({
            "unit_count": 2, "validity_counts": {"valid": 1, "superseded": 1},
            "superseded_ids": ["a"], "stale_ids": [], "expired_ids": [],
            "conflicts": [{"group": "g1", "kind": "policy_adaptation",
                           "unit_ids": ["a", "b"]}],
            "llm_fallback": False}, session_factory=_factory) == 2
        # 仅裁决事件 → 只写裁决行
        assert await record_evidence_shadow({
            "unit_count": 1, "validity_counts": {"stale": 1},
            "superseded_ids": [], "stale_ids": ["s1"], "expired_ids": [],
            "conflicts": [], "llm_fallback": True}, session_factory=_factory) == 1
        # 仅冲突事件 → 只写冲突行
        assert await record_evidence_shadow({
            "unit_count": 3, "validity_counts": {"valid": 3},
            "superseded_ids": [], "stale_ids": [], "expired_ids": [],
            "conflicts": [{"group": "g2", "kind": "stale_state",
                           "unit_ids": ["x"]}], "llm_fallback": False},
            session_factory=_factory) == 1
    asyncio.run(_run())

    by_suite = {}
    for obj in added:
        by_suite.setdefault(obj.suite, []).append(obj)
    assert set(by_suite) == {"evidence_view_shadow", "implicit_conflict_shadow"}
    for obj in added:
        assert obj.arm == "shadow"
        assert obj.run_id.split("-", 1)[0] in ("evshadow", "conflictshadow")
    a = by_suite["evidence_view_shadow"]
    assert [o.metrics_json["superseded_ids"] for o in a] == [["a"], []]
    assert [o.metrics_json["stale_ids"] for o in a] == [[], ["s1"]]
    assert a[1].metrics_json["llm_fallback"] is True
    c = by_suite["implicit_conflict_shadow"]
    assert [o.metrics_json["conflicts"][0]["kind"] for o in c] == \
        ["policy_adaptation", "stale_state"]


def test_record_evidence_shadow_metrics_allowlist():
    """持久化边界允许清单（B 评审 Important）：非法 validity 键/额外字段/
    未知 kind 不得原样落库。"""
    from app.services.memory_evidence_view_service import record_evidence_shadow

    added = []

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def add(self, obj):
            added.append(obj)

        async def commit(self):
            pass

    async def _run():
        assert await record_evidence_shadow({
            "unit_count": 1, "validity_counts": {"valid": 1, "泄密键": 99},
            "superseded_ids": ["a"], "stale_ids": [], "expired_ids": [],
            "conflicts": [{"group": "g", "kind": "整段正文泄漏",
                           "unit_ids": ["u"], "detail": "不该落库"}],
            "llm_fallback": False, "extra": "不该落库"},
            session_factory=_Sess) == 2
    asyncio.run(_run())
    for obj in added:
        assert set(obj.metrics_json) <= {"unit_count", "validity_counts",
                                         "superseded_ids", "stale_ids",
                                         "expired_ids", "conflicts",
                                         "llm_fallback"}
        assert "泄密键" not in obj.metrics_json.get("validity_counts", {})
        for c in obj.metrics_json.get("conflicts", []):
            assert set(c) == {"group", "kind", "unit_ids"}
            assert c["kind"] in ("stale_state", "premise_resistance",
                                 "policy_adaptation", "unknown")


def test_shadow_report_contract_shape():
    """Hub 形状钉（Consistency Hub T2 行）。"""
    cands = [{"id": c.id, "content": c.content, **c.metadata} for c in _conflict_cands()]
    rep = shadow_report(cands)
    assert set(rep) == {"unit_count", "validity_counts", "superseded_ids",
                        "stale_ids", "expired_ids", "conflicts"}
    assert set(rep["validity_counts"]) <= {"valid", "superseded", "expired", "stale"}
    assert build_evidence_units([]) == []
