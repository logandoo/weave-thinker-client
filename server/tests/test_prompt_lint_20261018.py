"""C-11: prompt lint must catch regressions beyond the adjacent-3-line scan."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.prompts.compiler import check_conflicts, lint_spec, lint_text
from app.prompts.spec import PromptSection, PromptSpec


def _spec(*sections, flags=None):
    return PromptSpec(
        id="test.spec", version="1.0.0", owner="tests",
        sections=tuple(sections), flags=flags or {},
    )


def test_duplicate_number_detected_beyond_adjacent_window():
    text = "1. a\n2. b\n3. c\n4. d\n5. e\n1. restarted"
    assert any("duplicate numbered" in issue for issue in lint_text(text))


def test_duplicate_number_across_sections_is_not_a_false_positive():
    spec = _spec(
        PromptSection(id="a", text="1. one\n2. two", tier="platform"),
        PromptSection(id="b", text="1. again", tier="platform"),
    )
    issues = lint_spec(spec)
    assert not any("duplicate numbered" in issue for issue in issues)


def test_blank_line_restart_of_non_one_is_flagged():
    assert any("duplicate numbered" in i
               for i in lint_text("7. seven\n\n7. seven again"))


def test_blank_line_restart_of_one_is_a_new_list():
    assert not any("duplicate numbered" in i
                   for i in lint_text("1. one\n\n1. new list"))


def test_nested_numbered_list_is_not_a_duplicate():
    text = "1. a\n   1. sub\n2. b\n   1. sub2"
    assert not any("duplicate numbered" in i for i in lint_text(text))


def test_sub_numbered_items_are_list_continuations():
    text = "1. a\n2. b\n3a. detail\n3. c\n4. d"
    assert not any("duplicate numbered" in i for i in lint_text(text))


def test_rule_conflict_requires_same_base_id():
    same = _spec(
        PromptSection(id="a", text="x", rule_ids=("SEC-TOOL-ALWAYS",), tier="safety_core"),
        PromptSection(id="b", text="y", rule_ids=("SEC-TOOL-NEVER",), tier="safety_core"),
    )
    assert check_conflicts(same), "same base id must conflict"

    unrelated = _spec(
        PromptSection(id="a", text="x", rule_ids=("SEC-A-ALWAYS",), tier="safety_core"),
        PromptSection(id="b", text="y", rule_ids=("SEC-B-NEVER",), tier="safety_core"),
    )
    assert check_conflicts(unrelated) == [], "unrelated ids are not a conflict"


def test_overrides_flag_keeps_conflict_silent():
    spec = _spec(
        PromptSection(id="a", text="x", rule_ids=("R-ALWAYS",), tier="platform"),
        PromptSection(id="b", text="y", rule_ids=("R-NEVER",), tier="platform"),
        flags={"overrides": True},
    )
    assert check_conflicts(spec) == []


def test_long_section_requires_budget():
    long_text = "中文" * 900  # > 1200 estimated tokens
    spec = _spec(PromptSection(id="long", text=long_text, tier="platform"))
    issues = lint_spec(spec)
    assert any("must declare budget_tokens" in issue for issue in issues), issues

    budgeted = _spec(PromptSection(
        id="long", text=long_text, tier="platform", budget_tokens=5000))
    assert not any("must declare budget_tokens" in i for i in lint_spec(budgeted))
