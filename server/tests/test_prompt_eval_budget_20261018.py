"""P2/P3 prompt eval harness + budget contracts (2026-10-18)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.prompts.budget import DEFAULT_BUDGETS, check_budget, total_system_tokens


def test_prompt_eval_golden_set_passes():
    from scripts.prompt_eval import run
    failures, total = run()
    assert total >= 30
    assert failures == []


def test_budget_detects_violation():
    report = check_budget({"memory": 999999, "tools_schema": 10})
    assert not report.ok
    assert any("memory" in v for v in report.violations)


def test_total_system_tokens_excludes_tools_schema():
    assert total_system_tokens({"memory": 10, "tools_schema": 999}) == 10


def test_default_budgets_present():
    assert "total_system" in DEFAULT_BUDGETS
    assert DEFAULT_BUDGETS["total_system"] > 0
