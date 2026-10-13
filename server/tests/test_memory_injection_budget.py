"""TDD tests: 记忆注入预算 —— Proteus（2608.16844 增量记忆激活）启发的
「早段垄断钳制」：单段超过总预算时不得独占全部容量，后续段保留新鲜容量。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_memory_injection_budget.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.memory_retrieval_service as mrs


def test_unchanged_when_sections_fit():
    assert mrs._apply_token_budget(["段一", "段二"], 100) == "段一\n\n段二"


def test_unchanged_accumulation_overflow_drops_tail():
    # 两段各 80、预算 100：旧语义——整段保留第一段、丢弃第二段并标注
    text, truncated, used = mrs._apply_token_budget_ex(["x" * 80, "y" * 80], 100)
    assert text.startswith("x" * 80)
    assert "y" not in text
    assert truncated is True
    assert used == 80


def test_monopoly_section_cannot_starve_later_sections():
    # Proteus：早段（单段超预算）不得独占容量——后续段保留新鲜容量
    text, truncated, used = mrs._apply_token_budget_ex(["X" * 1000, "Y" * 100], 200)
    assert text.startswith("X"), "高优先段仍保留"
    assert "Y" in text, "后续段必须保有新鲜容量（Proteus 增量激活）"
    assert truncated is True
    assert used <= 200


def test_monopoly_share_bounds_primary_section():
    text, _truncated, used = mrs._apply_token_budget_ex(["X" * 1000], 200, monopoly_share=0.7)
    assert used == 140, "单段最多占 share 份额"
    assert text.startswith("X" * 140)


def test_monopoly_residual_capacity_goes_to_later_sections():
    # [小段, 超预算大段, 小段]：大段被钳制后，前后小段都在预算内保住
    text, truncated, used = mrs._apply_token_budget_ex(
        ["A" * 30, "X" * 1000, "B" * 30], 200,
    )
    assert text.startswith("A" * 30)
    assert "X" in text
    assert "B" * 30 in text, "垄断钳制后的残余容量必须留给后续段"
    assert truncated is True
    assert used <= 200


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
