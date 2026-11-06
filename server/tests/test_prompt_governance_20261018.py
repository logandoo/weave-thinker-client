"""P1.6/P6 governance checks: coverage, manifest, lint, golden set, calibration."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.prompts.registry import all_specs, manifest, unused_prompts


def test_manifest_has_owner_version_and_used_by_for_every_spec():
    rows = manifest()
    assert len(rows) == len(all_specs())
    for row in rows:
        assert row["id"] and row["version"] and row["owner"]
        assert row["used_by"], row["id"]
        assert row["hash"], row["id"]
        assert row["locale"] in ("zh", "en")


def test_changelog_lists_every_spec():
    from pathlib import Path
    from app.prompts.registry import all_specs

    text = (Path(__file__).resolve().parents[2] / "docs" / "PROMPT_CHANGELOG.md").read_text(
        encoding="utf-8")
    missing = [s.id for s in all_specs() if f"`{s.id}`" not in text]
    assert missing == []


def test_no_dead_prompts():
    assert unused_prompts() == []


def test_coverage_is_100_percent():
    from scripts.prompt_audit import inventory

    data = inventory()
    assert data["system_site_count"] == 51
    assert data["unmapped_count"] == 0
    assert data["mapped_count"] == data["system_site_count"]


def test_lint_is_clean():
    from scripts.prompt_audit import lint

    assert lint() == []


def test_golden_set_has_nine_categories_and_passes():
    import json
    from pathlib import Path
    from scripts.prompt_eval import run

    failures, total = run()
    assert total >= 30
    assert failures == []
    cases = json.loads(
        (Path(__file__).resolve().parents[1] / "tests" / "prompt_evals" / "cases.json")
        .read_text(encoding="utf-8"))
    categories = {c.get("category") for c in cases if c.get("category")}
    assert len(categories) >= 9, categories


def test_judge_calibration_seed_meets_threshold():
    from scripts.prompt_judge_calibration import agreement, load_labels, rubric_judge

    rows = load_labels()
    predicted = {row["id"]: rubric_judge(row) for row in rows}
    rate, mismatches = agreement(rows, predicted)
    assert rate >= 0.85, mismatches
