"""W1-3 taint 继承（记忆/长程 v2，2026-10-23，判据 #9）：四条派生链路
（摘要/恢复包/梦境/证据单元）继承来源最严 taint，不得洗白。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_derived_taint_inheritance.py -v
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import taint_service  # noqa: E402


class _FakeResult:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeDB:
    def __init__(self, owner_row=("u1",)):
        self._owner = owner_row
        self.executed = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return _FakeResult(self._owner)

    async def commit(self):
        return None


class TestDerivedTaintRule:
    """`_record_derived_provenance` 的继承规则（摘要/恢复包/压缩摘要共用）。"""

    def _run(self, ledger):
        from app.services import context_continuity_service as ccs

        captured = {}

        async def fake_load_ledger(db, cid, uid):
            return ledger

        async def fake_record(db, uid, atype, aid, taint, **kw):
            captured["taint"] = taint
            captured["origin"] = kw.get("origin")

        orig = (taint_service.load_ledger, taint_service.record_artifact_taint,
                taint_service.provenance_enabled)
        taint_service.load_ledger = fake_load_ledger
        taint_service.record_artifact_taint = fake_record
        taint_service.provenance_enabled = lambda: True
        try:
            asyncio.run(ccs._record_derived_provenance(
                _FakeDB(), "conv1", "summary"))
        finally:
            (taint_service.load_ledger, taint_service.record_artifact_taint,
             taint_service.provenance_enabled) = orig
        return captured

    def test_untrusted_ledger_stays_untrusted(self):
        ledger = SimpleNamespace(loaded=True, max_taint="untrusted", user_id="u1")
        cap = self._run(ledger)
        assert cap["taint"] == "untrusted", "派生不得洗白 untrusted"
        assert cap["origin"] == "untrusted_external"

    def test_trusted_ledger_derived_is_agent_not_trusted(self):
        ledger = SimpleNamespace(loaded=True, max_taint="trusted", user_id="u1")
        cap = self._run(ledger)
        assert cap["taint"] == "agent", "派生记忆至少 agent（摘要非用户原话）"

    def test_unloaded_ledger_is_untrusted(self):
        ledger = SimpleNamespace(loaded=False, max_taint="trusted", user_id="u1")
        cap = self._run(ledger)
        assert cap["taint"] == "untrusted"

    def test_no_ledger_no_record(self):
        cap = self._run(None)
        assert cap == {}, "无 ledger 时不落账（fail-closed 于写点外）"


class TestCompactionProvenanceHook:
    """压缩摘要链路：CompressionMixin 挂 derived_provenance_hook 并以
    artifact_type='compaction' 记账。"""

    def test_attach_sets_hook(self):
        from app.services.agent_loop_compression import CompressionMixin

        fake_self = SimpleNamespace(
            workspace_path="/tmp/ws", _delivery_conv_id="conv1",
            vibeweaver_active=False)
        fake_self._record_compaction_provenance = (
            lambda cid: CompressionMixin._record_compaction_provenance(fake_self, cid))
        compressor = SimpleNamespace(delivery_root=None, delivery_conv_id="",
                                     delivery_active=True)
        CompressionMixin._attach_compressor_delivery(fake_self, compressor)
        assert getattr(compressor, "derived_provenance_hook", None) is not None

    def test_hook_records_compaction_taint(self):
        from app.services import agent_loop_compression as alc

        calls = []

        async def fake_record(db, cid, atype):
            calls.append((cid, atype))

        class _FakeSessionCtx:
            async def __aenter__(self):
                return _FakeDB()

            async def __aexit__(self, *a):
                return False

        fake_self = SimpleNamespace()
        import app.services.context_continuity_service as ccs
        import app.db.database as dbmod
        orig = ccs._record_derived_provenance
        ccs._record_derived_provenance = fake_record
        orig_db = dbmod.AsyncSessionLocal
        dbmod.AsyncSessionLocal = lambda: _FakeSessionCtx()
        try:
            asyncio.run(alc.CompressionMixin._record_compaction_provenance(
                fake_self, "conv9"))
        finally:
            ccs._record_derived_provenance = orig
            dbmod.AsyncSessionLocal = orig_db
        assert calls == [("conv9", "compaction")]


class TestEvidenceUnitTaint:
    """证据单元链路：来源决定基线 taint，取最严（工具输出=untrusted）。"""

    def test_verifier_judge_sources_are_agent(self):
        from app.services.deathmatch_evidence import evidence_unit_taint

        assert evidence_unit_taint("verifier") == "agent"
        assert evidence_unit_taint("judge") == "agent"

    def test_unknown_tool_source_is_untrusted(self):
        from app.services.deathmatch_evidence import evidence_unit_taint

        assert evidence_unit_taint("unknown") == "untrusted"
        assert evidence_unit_taint(None) == "untrusted"

    def test_record_unit_carries_taint(self, monkeypatch):
        from app.services.deathmatch_evidence import DeathmatchEvidenceMixin

        # 单元形状含 taint 字段（继承最严）；构造最小 mixin 实例验证写点
        monkeypatch.setattr(
            "app.services.deathmatch_write_through.write_through_bg",
            lambda *a, **kw: None)
        ctx = DeathmatchEvidenceMixin.__new__(DeathmatchEvidenceMixin)
        ctx._conv = type("C", (), {"deathmatch_evidence_units": []})()
        ctx._EVIDENCE_UNIT_KINDS = ("OBSERVED", "FACT", "FAIL", "CLAIM",
                                    "PATCH_SUMMARY")
        ctx._EVIDENCE_UNITS_CAP = 200
        ctx._EVIDENCE_UNIT_TEXT_CHARS = 300
        unit = ctx._record_evidence_unit("OBSERVED", "unknown", "看到 X",
                                         step_id="s1")
        assert unit is not None and "taint" in unit
        assert unit["taint"] == "untrusted"
