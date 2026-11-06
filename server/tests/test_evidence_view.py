"""W2-1/W2-2（记忆/长程 v2，2026-10-23，判据 #11/#12）：证据视图四元组 +
确定性裁决 + 隐式冲突三维度（规则优先，LLM 兜底默认关）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import memory_evidence_view_service as evs  # noqa: E402


def _units():
    return evs.build_evidence_units([
        {"id": "a", "content": "站会在 09:30", "valid_from": "2026-10-01",
         "created_at": "2026-10-01", "source_trust": "user_stated"},
        {"id": "b", "content": "站会改在 10:15", "valid_from": "2026-10-05",
         "created_at": "2026-10-05", "source_trust": "user_stated"},
    ])


class TestEvidenceUnits:
    def test_fields_complete(self):
        u = evs.build_evidence_units([{
            "id": "x", "content": "事实", "valid_from": "2026-10-01",
            "created_at": "2026-10-02", "participants": "甲",
            "conversation_id": "c1"}])[0]
        for k in ("fact", "source", "event_time", "mention_time",
                  "session_span", "participants", "validity", "conflict_group"):
            assert k in u, k
        assert u["event_time"] == "2026-10-01"
        assert u["mention_time"] == "2026-10-02"
        assert u["session_span"] == "c1"

    def test_superseded_and_expired(self):
        units = evs.build_evidence_units([
            {"content": "x", "superseded": True},
            {"content": "y", "valid_to": "2020-01-01"},
        ])
        assert units[0]["validity"] == "superseded"
        assert units[1]["validity"] == "expired"

    def test_future_valid_to_stays_valid(self):
        """真 bug 回归（评审套件暴露）：valid_to 在未来 ≠ 过期。"""
        units = evs.build_evidence_units([{"content": "z", "valid_to": "2999-01-01"}])
        assert units[0]["validity"] == "valid"

    def test_deterministic_idempotent(self):
        a = evs.adjudicate_conflicts(_units())
        b = evs.adjudicate_conflicts(_units())
        assert a == b


class TestAdjudication:
    def test_newest_valid_wins(self):
        out = evs.adjudicate_conflicts(_units())
        by_id = {u["unit_id"]: u for u in out}
        assert by_id["b"]["validity"] == "valid"
        assert by_id["a"]["validity"] == "superseded"

    def test_all_expired_keeps_newest_stale(self):
        units = evs.build_evidence_units([
            {"content": "同", "valid_to": "2020-01-01", "created_at": "2026-01-01"},
        ])
        out = evs.adjudicate_conflicts(units)
        assert out[0]["validity"] == "stale"


class TestImplicitConflict:
    def test_stale_state(self):
        out = evs.detect_implicit_conflict(_units())
        assert out and out[0]["kind"] in ("stale_state", "policy_adaptation")

    def test_premise_resistance(self):
        units = evs.build_evidence_units([
            {"content": "不要自动删日志", "valid_from": "2026-10-01"},
            {"content": "自动删日志", "valid_from": "2026-10-02"},
        ])
        out = evs.detect_implicit_conflict(units)
        assert out and out[0]["kind"] == "premise_resistance"

    def test_no_conflict_single_fact(self):
        assert evs.detect_implicit_conflict(
            evs.build_evidence_units([{"content": "唯一", "valid_from": "t"}])) == []
