"""Behavioural coverage for the C-01c audit-branch split (2026-10-20).

The split of `_finalize_primary_audit_branch` into
`_primary_audit_guidance` / `_primary_audit_verdict` left the audit path with
static gates only (line bounds, arity, write-through).  A reviewer caught a
real defect there (call site passed 2 of 4 args) precisely because no test
executed the branch.  These tests drive it end-to-end with stubbed LLM calls so
the three settlements are exercised:

  * legacy reject  → `audit_reset` + `_LoopContinueSignal` + counter bump
  * budget spent   → bounded salvage, `salvaged_final`, no continue
  * advisory pass  → guidance consumed silently into `_shipped_*`

Run: cd backend && python -m pytest tests/test_finalize_audit_branch_20261020.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import agent_loop_orchestrator as orch
from app.services.agent_loop import AuditVerdict
from app.services.agent_loop_orchestrator import (  # noqa: E402
    AgentLoopOrchestratorMixin,
    _LoopContinueSignal,
    _LoopResult,
)


class _State:
    def __init__(self) -> None:
        self.creative_turn = False
        self.skip_audit_once = False
        self.search_required = False
        self.audit_rejections = 0
        self.audit_soft_rejections = 0
        self.audit_rejected_drafts: list = []
        self.revision_rewrite_flagged = False
        self.retry_thinking_off = False
        self.audit_advisory_shipped = False
        self.audit_accepted = False
        self.audit_fail_families: list = []
        self.npg_reject_flags: list = []
        self.npg_reject_tool_calls: list = []
        self.npg_reject_raws: list = []
        self.turn_content_segments: list = []
        self.messages: list = [{"role": "user", "content": "问题"}]
        self.salvaged_final = False
        self.draft_attempt_seq = 0


class _Engine(AgentLoopOrchestratorMixin):
    enable_reasoning = False
    provider_type = "deepseek"
    on_draft_rejected = None
    reasoning_effort = ""

    def __init__(self, verdict: AuditVerdict) -> None:
        self._next_verdict = verdict
        self.audit_calls = 0

    def _skip_guardrails(self) -> bool:
        return False

    def _live_thinking_enabled(self) -> bool:
        return True

    async def _audit_response(self, state, target, last_user_msg):
        self.audit_calls += 1
        return self._next_verdict

    def _advisory_audit_gate(self, *args, **kwargs):
        return "pass"

    def _advisory_ship_events(self, state, guidance, base_text):
        return ([], base_text)

    async def _salvage_after_audit_budget(self, state, last_user_msg):
        yield {"content": "抢救稿"}


def _result(**fields) -> _LoopResult:
    res = _LoopResult()
    res.assistant_content = fields.pop("assistant_content", "草稿正文")
    res.guidance = None
    res._audit_target = ""
    res._shipped_guidance = None
    res._shipped_text = None
    res._audit_reject_budget = 0
    res._soft_limit = 0
    res._stitched_audit = False
    res._prev_stash = None
    res._ships_as_final = False
    res._budget_spent = False
    res._npg_no_progress = False
    res._family_stall = False
    res._audit_family = ""
    res._salvage_parts = []
    res._adv_events = []
    res._adv_text = ""
    res._cov_note = None
    res._lc_note = None
    for key, value in fields.items():
        setattr(res, key, value)
    return res


def _config(**overrides):
    base = dict(
        agent_audit_policy="legacy",
        agent_audit_soft_reject_limit=5,
        agent_audit_reject_budget=5,
        agent_audit_reject_budget_search=5,
        agent_audit_revision_min_similarity=0.0,
        agent_audit_revision_thinking_enabled=True,
        agent_audit_stall_cut_family_repeats=3,
    )
    base.update(overrides)
    return type("Cfg", (), base)()


async def _drain(gen):
    events = []
    try:
        async for event in gen:
            events.append(event)
    except _LoopContinueSignal:
        return events, "continue"
    return events, "end"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Stub the module-level helpers the branch calls so the test covers the
    branch's own control flow (and its call signatures), not the helpers."""
    orch._sync_agent_globals()
    monkeypatch.setattr(orch, "config", _config(), raising=False)
    monkeypatch.setattr(orch, "_audit_reject_budget_for", lambda state: 5, raising=False)
    monkeypatch.setattr(orch, "_stash_rejected_draft",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(orch, "_prune_guardrail_pairs",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(orch, "_rejected_append",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(orch, "_inject_directive",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(orch, "_consume_low_confidence_note",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(orch, "_draft_ships_as_final",
                        lambda *a, **k: True, raising=False)
    monkeypatch.setattr(orch, "_audit_problem_family",
                        lambda *a, **k: "family-x", raising=False)
    monkeypatch.setattr(orch, "_audit_family_stalled",
                        lambda *a, **k: False, raising=False)
    monkeypatch.setattr(orch, "_npg_soft_reject_no_progress",
                        lambda *a, **k: False, raising=False)

    async def _emit(*a, **k):
        return None

    monkeypatch.setattr(orch, "_emit_draft_rejected", _emit, raising=False)
    yield


def test_legacy_reject_resets_and_asks_for_another_iteration():
    engine = _Engine(AuditVerdict(verdict="reject", problem="无依据",
                                  guidance="请补依据"))
    state, res = _State(), _result()

    events, how = asyncio.run(_drain(
        engine._finalize_primary_audit_branch(state, res, _last_user_msg="问题")))

    assert how == "continue", "reject must ask for another iteration"
    assert any(ev.get("audit_reset") for ev in events), events
    assert state.audit_rejections == 1
    assert res.guidance is not None and res.guidance.verdict == "reject"
    assert engine.audit_calls == 1


def test_budget_spent_goes_to_salvage_and_never_ships_the_rejected_draft():
    engine = _Engine(AuditVerdict(verdict="reject", problem="截断", guidance="重写"))
    state, res = _State(), _result()
    state.audit_rejections = 5          # == budget -> spent after the bump

    events, how = asyncio.run(_drain(
        engine._finalize_primary_audit_branch(state, res, _last_user_msg="问题")))

    assert how == "end"
    assert state.salvaged_final is True
    assert res.assistant_content == "抢救稿"
    assert any(ev.get("content") == "抢救稿" for ev in events), events
    assert not any(ev.get("audit_reset") for ev in events), \
        "salvage path must not reset the client timeline"


def test_advisory_policy_consumes_soft_verdict_silently():
    engine = _Engine(AuditVerdict(verdict="needs_evidence", problem="覆盖不足",
                                  guidance="补证据"))
    state, res = _State(), _result()
    orch.config = _config(agent_audit_policy="advisory")   # type: ignore[assignment]

    events, how = asyncio.run(_drain(
        engine._finalize_primary_audit_branch(state, res, _last_user_msg="问题")))

    assert how == "end"
    assert res.guidance is None, "advisory consumes the verdict"
    assert res._shipped_guidance is not None
    assert res._shipped_text == "草稿正文"
    assert state.audit_rejections == 0
    assert engine.audit_calls == 1
