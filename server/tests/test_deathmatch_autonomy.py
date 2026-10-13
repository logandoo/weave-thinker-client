"""TDD tests: 死磕自治（autonomy）波 — 默认解除自动人工门 + 补充即恢复 + 停泊语义.

Criteria map: tests/acceptance.md items 1-12, 14 (unit-level).
Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_deathmatch_autonomy.py -v
"""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.deathmatch_service as ds
from app.services.deathmatch_service import DeathmatchManager
from app.core.config import Config


# ── fixtures ──────────────────────────────────────────────────────────────

def _conv(**over):
    base = dict(
        id="conv-auto",
        deathmatch_mode=True,
        deathmatch_status="active",
        deathmatch_goal="测试目标",
        deathmatch_turns=0,
        deathmatch_max_turns=0,
        deathmatch_verify_failures=0,
        deathmatch_consecutive_failures=0,
        deathmatch_verdict=None,
        deathmatch_reason=None,
        deathmatch_plan={"steps": [
            {"id": "s1", "status": "in_progress", "description": "步骤1", "expected_output": "a.txt"},
        ]},
        deathmatch_plan_version=1,
        deathmatch_reflections=[],
        deathmatch_last_verification_result=None,
        deathmatch_wall_time_started_at=None,
        deathmatch_max_wall_time_seconds=0,
        deathmatch_wall_time_used_seconds=0,
        deathmatch_grilling_qa_history=[],
        deathmatch_grilling_complete=True,
        deathmatch_human_gate=None,
        deathmatch_bible_draft={"x": 1},
        deathmatch_settled_ledger=None,
        deathmatch_subgoals=[],
    )
    base.update(over)
    return SimpleNamespace(**base)


def _mgr(conv, judge=("continue", "not done", False, False, False)):
    mgr = DeathmatchManager(conv)
    mgr.get_continuation_prompt = lambda last_response="": "cont"
    mgr._record_reflection = lambda *a, **k: None
    mgr._record_settled = lambda *a, **k: None
    mgr.collect_final_deliverables_from_messages = lambda *a, **k: asyncio.sleep(0, result=[])
    mgr._maybe_harness_repair = lambda *a, **k: asyncio.sleep(0, result=False)
    # AEWM 波（2609.28416）：停滞分诊也是外部 LLM 缝——同 _maybe_harness_repair
    # 约定伪造（真实调用在单测环境走网络：108s/次且路由非确定）。
    mgr._triage_stall_actions = lambda *a, **k: asyncio.sleep(0, result=None)
    ds._call_judge_llm = lambda goal, resp, **k: asyncio.sleep(0, result=judge)
    return mgr


@pytest.fixture(autouse=True)
def _restore_judge():
    orig = ds._call_judge_llm
    yield
    ds._call_judge_llm = orig


@pytest.fixture
def autonomy_off():
    dm = ds.config._config.setdefault("deathmatch", {})
    _prev = dm.get("autonomy_enabled")
    dm["autonomy_enabled"] = False
    yield
    if _prev is None:
        dm.pop("autonomy_enabled", None)
    else:
        dm["autonomy_enabled"] = _prev


@pytest.fixture
def legacy_breakers_off():
    """W2 orthogonal breakers OFF for the stall-tier/autonomy contract tests:
    the global no-progress replan breaker and per-node recovery have their own
    coverage in test_deathmatch_contract_v2.py (added 2026-09-18; this fixture
    isolates pre-existing behavior, it does not weaken any assertion).

    Save/restore the EXACT prior values (2026-09-20): unconditional pop deleted
    config.toml-provided values (no_progress_replan_cap=3, node_recovery_max_
    retries=2) and broke later tests in the same session."""
    dm = ds.config._config.setdefault("deathmatch", {})
    _prev_cap = dm.get("no_progress_replan_cap")
    _prev_retries = dm.get("node_recovery_max_retries")
    dm["no_progress_replan_cap"] = 0
    dm["node_recovery_max_retries"] = 10 ** 9
    yield
    if _prev_cap is None:
        dm.pop("no_progress_replan_cap", None)
    else:
        dm["no_progress_replan_cap"] = _prev_cap
    if _prev_retries is None:
        dm.pop("node_recovery_max_retries", None)
    else:
        dm["node_recovery_max_retries"] = _prev_retries


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ── Criterion 1/2: config defaults are unlimited + autonomy flag ─────────

def test_defaults_unlimited():
    with tempfile.TemporaryDirectory() as td:
        cfg_path = os.path.join(td, "config.toml")
        with open(cfg_path, "w") as fh:
            fh.write('[server]\nhost="127.0.0.1"\nport=8158\n[database]\nurl="x"\n')
        fresh = Config(cfg_path)
    assert fresh.deathmatch_max_wall_time_seconds == 0, "wall default must be 0 (unlimited)"
    assert fresh.deathmatch_max_turns == 0, "turns default must be 0 (unlimited)"
    assert getattr(fresh, "deathmatch_autonomy_enabled", "MISSING") is True, (
        "autonomy_enabled property must exist and default True"
    )


# ── Criterion 1: wall_time_exceeded unlimited semantics ──────────────────

def test_wall_time_unlimited():
    conv = _conv(deathmatch_max_wall_time_seconds=0, deathmatch_wall_time_used_seconds=10**9)
    assert DeathmatchManager(conv).wall_time_exceeded() is False, "0 column = unlimited"


def test_wall_time_legacy_cap_still_works():
    conv = _conv(deathmatch_max_wall_time_seconds=10, deathmatch_max_turns=0,
                 deathmatch_wall_time_used_seconds=11)
    assert DeathmatchManager(conv).wall_time_exceeded() is True, "positive cap still enforced"


# ── Criterion 2: turn budget 0 = unlimited ───────────────────────────────

def test_turn_budget_unlimited():
    conv = _conv(deathmatch_max_turns=0, deathmatch_turns=500)
    mgr = _mgr(conv)
    mgr.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "partial", "progress": True, "current_step": "s1", "issues": [], "completed_steps": []})
    d = _run(mgr.evaluate_after_turn("working", workspace_path="/tmp/x"))
    assert d["should_continue"] is True and d["status"] != "human_gate", d


# ── Criterion 3: autonomy stall never gates; episode resets at hard ──────

def test_stall_autonomy_never_gates(legacy_breakers_off):
    conv = _conv()
    mgr = _mgr(conv)
    replan_calls = []
    mgr.replan = lambda vr: asyncio.sleep(0, result=replan_calls.append(1) or None)
    mgr.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "partial", "progress": False, "current_step": "s1", "issues": [], "completed_steps": []})
    hard = ds.config.deathmatch_stall_hard_threshold
    total = hard + 4
    for i in range(total):
        d = _run(mgr.evaluate_after_turn("working", workspace_path="/tmp/x"))
        assert d["should_continue"] is True, f"stall round {i+1} must continue in autonomy: {d}"
        assert d["status"] == "active", f"stall round {i+1} must stay active: {d}"
        assert conv.deathmatch_status == "active"
    assert replan_calls, "autonomy must keep replanning"
    assert conv.deathmatch_verify_failures == 4, (
        f"episode must reset at hard threshold ({hard}): expected counter 4, got "
        f"{conv.deathmatch_verify_failures}"
    )


# ── Criterion 4: spin guard autonomy continues with directive ────────────

def test_spin_guard_autonomy_continues():
    ds._SPIN_COUNTS.pop("conv-auto", None)
    conv = _conv()
    mgr = _mgr(conv)
    mgr.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "partial", "progress": True, "current_step": "s1", "issues": [], "completed_steps": []})
    d1 = _run(mgr.evaluate_after_turn("", workspace_path="/tmp/x", tool_results=None))
    assert d1["should_continue"] is True
    d2 = _run(mgr.evaluate_after_turn("", workspace_path="/tmp/x", tool_results=None))
    assert d2["should_continue"] is True, "spin guard must not gate in autonomy"
    assert d2["status"] == "active" and conv.deathmatch_status == "active"
    assert "实质" in (d2.get("continuation_prompt") or ""), "spin directive must demand substance"
    ds._SPIN_COUNTS.pop("conv-auto", None)


# ── Criterion 5: judge WAIT → bounded in-loop backoff + continue ─────────

def test_wait_autonomy_backoff_continue():
    conv = _conv()
    mgr = _mgr(conv, judge=("wait", "blocked on async (wait_seconds=30)", False, False, False))
    d = _run(mgr.evaluate_after_turn("waiting on background task", workspace_path=""))
    assert d["verdict"] == "wait"
    assert d["should_continue"] is True, "autonomy WAIT must not park the run"
    assert d.get("backoff_seconds") == 30, f"backoff must come from judge wait_seconds: {d}"
    assert conv.deathmatch_status == "active", "status stays active (no park)"


def test_wait_autonomy_backoff_capped():
    conv = _conv()
    mgr = _mgr(conv, judge=("wait", "blocked (wait_seconds=3600)", False, False, False))
    d = _run(mgr.evaluate_after_turn("waiting", workspace_path=""))
    assert d.get("backoff_seconds") == 120, f"backoff must clamp to 120s: {d}"


# ── Criterion 7: judge infra streak still pauses (environment failure) ───

def test_infra_streak_autonomy_pauses():
    ds._JUDGE_INFRA_FAILURES.pop("conv-auto", None)
    conv = _conv()
    mgr = _mgr(conv, judge=("continue", "timeout", False, True, False))
    d1 = _run(mgr.evaluate_after_turn("working", workspace_path=""))
    assert d1["status"] != "paused", "first infra failure must not pause"
    d2 = _run(mgr.evaluate_after_turn("working", workspace_path=""))
    assert d2["status"] == "paused" and d2["should_continue"] is False, d2
    assert conv.deathmatch_status == "paused"
    assert "infra" in (conv.deathmatch_reason or "")
    ds._JUDGE_INFRA_FAILURES.pop("conv-auto", None)


# ── Criterion 8: judge parse-failure streak still pauses (regression) ────

def test_parse_failures_still_pause():
    conv = _conv()
    mgr = _mgr(conv, judge=("continue", "garbage", True, False, False))
    maxc = ds.config.deathmatch_max_consecutive_failures
    d = None
    for _ in range(maxc):
        d = _run(mgr.evaluate_after_turn("working", workspace_path=""))
    assert d["status"] == "paused" and conv.deathmatch_status == "paused"


# ── Criterion 9: resume re-snapshots limits (legacy 3600 column heals) ───

def test_resume_resnapshots_limits():
    conv = _conv(deathmatch_status="paused", deathmatch_mode=False,
                 deathmatch_max_wall_time_seconds=3600, deathmatch_max_turns=30)
    DeathmatchManager(conv).resume()
    assert conv.deathmatch_status == "active"
    assert conv.deathmatch_mode is True, "resume re-enables a parked task"
    assert conv.deathmatch_max_wall_time_seconds == ds.config.deathmatch_max_wall_time_seconds
    assert conv.deathmatch_max_turns == ds.config.deathmatch_max_turns


# ── Criterion 12: deactivate parks (state preserved) ─────────────────────

def test_deactivate_parks_task():
    conv = _conv(deathmatch_status="active")
    goal_before = conv.deathmatch_goal
    DeathmatchManager(conv).deactivate()
    assert conv.deathmatch_mode is False
    assert conv.deathmatch_status == "paused", "parked task must be resumable (paused)"
    assert conv.deathmatch_goal == goal_before, "goal must be preserved (park, not wipe)"
    assert conv.deathmatch_plan is not None, "plan must be preserved"


def test_activate_grilling_clears_parked_state():
    conv = _conv(deathmatch_status="paused")
    DeathmatchManager(conv).activate_grilling()
    assert conv.deathmatch_status == "grilling"
    assert conv.deathmatch_goal is None and conv.deathmatch_plan is None
    assert conv.deathmatch_bible_draft is None


# ── A4.9 W1-I1: backoff consumed on EVERY hot-continue decision site ─────

def test_backoff_consumed_on_all_continue_sites():
    """Structural pin: every deathmatch decision-continue block in agent_loop
    must consume _dm_decision_backoff (the W1-I1 omission class: budget /
    mid-loop paths hot-continued a WAIT verdict with zero delay)."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "services", "agent_loop.py")).read()
    sites = src.count("await self._dm_decision_backoff(")
    assert sites == 5, (
        f"expected 5 consumption sites (budget-exhaustion, inactivity, mid-loop, "
        f"main, recovery no-tool), found {sites}"
    )
    # The budget-exhaustion block specifically must contain it (A4.9 W1-I1).
    budget_block = src.split("Deathmatch budget exhausted")[1][:2600]
    assert "_dm_decision_backoff" in budget_block, "budget-exhaustion path must consume backoff"
    # The post-loop terminal path (grace_call + return) is exempt by ruling:
    # the run ends there — no hot loop, a sleep would only delay the summary.


def test_backoff_helper_clamps_and_sleeps():
    """Behavior: the helper sleeps the given backoff and clamps at 120s."""
    import time
    from app.services.agent_loop import AgentLoop
    t0 = time.monotonic()
    asyncio.run(AgentLoop._dm_decision_backoff(object(), {"backoff_seconds": 0.05}))
    assert time.monotonic() - t0 >= 0.04, "helper must actually sleep"
    asyncio.run(AgentLoop._dm_decision_backoff(object(), {"backoff_seconds": "bad"}))
    asyncio.run(AgentLoop._dm_decision_backoff(object(), {}))


# ── Criterion 14: autonomy OFF = legacy gating preserved ─────────────────

def test_autonomy_off_legacy_stall_gates(autonomy_off, legacy_breakers_off):
    # Legacy-valid limits (0 was not a valid value before the autonomy wave).
    conv = _conv(deathmatch_max_turns=9999, deathmatch_max_wall_time_seconds=None)
    mgr = _mgr(conv)
    mgr.replan = lambda vr: asyncio.sleep(0, result=None)
    mgr.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "partial", "progress": False, "current_step": "s1", "issues": [], "completed_steps": []})
    partial_at = ds.config.deathmatch_stall_partial_threshold
    d = None
    for _ in range(partial_at):
        d = _run(mgr.evaluate_after_turn("working", workspace_path="/tmp/x"))
    assert d["status"] == "partial_complete" and d["should_continue"] is False, d
    assert conv.deathmatch_status == "partial_complete"


def test_autonomy_off_legacy_wait_parks(autonomy_off):
    conv = _conv()
    mgr = _mgr(conv, judge=("wait", "blocked (wait_seconds=30)", False, False, False))
    d = _run(mgr.evaluate_after_turn("waiting", workspace_path=""))
    assert d["should_continue"] is False and d["verdict"] == "wait"
    assert not d.get("backoff_seconds"), "legacy WAIT parks without in-loop backoff"


def test_autonomy_off_legacy_spin_gates(autonomy_off):
    ds._SPIN_COUNTS.pop("conv-auto", None)
    conv = _conv(deathmatch_max_turns=9999, deathmatch_max_wall_time_seconds=None)
    mgr = _mgr(conv)
    mgr.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "partial", "progress": True, "current_step": "s1", "issues": [], "completed_steps": []})
    _run(mgr.evaluate_after_turn("", workspace_path="/tmp/x", tool_results=None))
    d2 = _run(mgr.evaluate_after_turn("", workspace_path="/tmp/x", tool_results=None))
    assert d2["status"] == "partial_complete" and d2["should_continue"] is False
    ds._SPIN_COUNTS.pop("conv-auto", None)
