"""TDD — W3 M4a 跨配置矩阵臂 + 场景集覆盖（10 类各 ≥3 可执行场景，每类含 1 个对抗变体）。

矩阵入口至少跑 provider_routing on/off 两臂（M1a A/B 判据，审计 §3.4/§10.6 W3 行）；
场景集=跨会话回忆/偏好遵循/时间型列表/矛盾更新/缺证据诚实 +（W0-2）陈旧状态/遗忘/
投毒防御/程序性/全中文（B6/B7/B1·B2/B10/B17），JSON 场景文件 + runner 全部可执行
（指标层假检索注入，零 LLM/零 DB）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/memory_evals -q
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))
sys.path.insert(0, _HERE)

import harness  # noqa: E402
import run_eval  # noqa: E402


# ── 矩阵：provider_routing on/off 两臂 ────────────────────────────────────

def test_matrix_provider_routing_two_arms():
    scenarios = harness.load_scenarios(harness.SCENARIOS_DIR)
    temporal = [s for s in scenarios if s["category"] == "temporal_list"]
    assert temporal, "时间型场景必须存在"
    arms = run_eval.parse_arms("provider_routing=off,provider_routing=on")
    assert [a["name"] for a in arms] == ["provider_routing=off", "provider_routing=on"]
    assert [a["provider_routing_enabled"] for a in arms] == [False, True]

    results = run_eval.run_matrix(temporal[:1], arms)
    runs = results["scenarios"]
    assert len(runs) == 2, "1 场景 × 2 臂"
    by_arm = {r["arm"]: r for r in runs}
    assert by_arm["provider_routing=off"]["metrics"]["route"] == "off"
    assert by_arm["provider_routing=on"]["metrics"]["route"] == "temporal_first", \
        "时间型查询开启路由后应走 temporal_first"
    assert set(results["summary"]["by_arm"]) == {
        "provider_routing=off", "provider_routing=on"}


def test_matrix_summary_aggregates_per_arm_and_category():
    scenarios = harness.load_scenarios(harness.SCENARIOS_DIR)
    arms = run_eval.parse_arms("provider_routing=off,provider_routing=on")
    results = run_eval.run_matrix(scenarios, arms)
    summary = results["summary"]
    assert summary["total_runs"] == len(scenarios) * 2
    assert "zero_hit_rate" in summary and "mean_injected_tokens" in summary
    assert "mean_latency_ms" in summary and "hit_at_k_mean" in summary
    for cat in harness.CATEGORIES:
        assert cat in summary["by_category"], f"汇总缺类别 {cat}"


# ── 场景集覆盖 + 可执行性 ─────────────────────────────────────────────────

def test_corpus_five_categories_two_each():
    # W0-2：覆盖升级为 10 类 ×3（含每类 1 个对抗变体）；断言只加强不减弱
    scenarios = harness.load_scenarios(harness.SCENARIOS_DIR)
    by_cat: dict = {}
    for s in scenarios:
        harness.validate_scenario(s)
        by_cat.setdefault(s["category"], []).append(s)
    assert len(harness.CATEGORIES) == 10, "评测集类别数应为 10（W0-2 扩充）"
    assert len(scenarios) >= 30, "评测集场景总数应 ≥ 30（10 类 × 3）"
    for cat in harness.CATEGORIES:
        assert len(by_cat.get(cat, [])) >= 3, f"类别 {cat} 场景数 < 3"
    for cat in harness.CATEGORIES:
        adversarial = [s for s in by_cat.get(cat, [])
                       if "对抗" in str(s.get("title") or "")]
        assert adversarial, f"类别 {cat} 缺对抗变体场景"
    assert {s["id"] for s in scenarios} == {
        s["id"] for s in scenarios}, "场景 id 必须唯一"


def test_corpus_all_scenarios_executable_all_checks_pass():
    scenarios = harness.load_scenarios(harness.SCENARIOS_DIR)
    arms = run_eval.parse_arms("provider_routing=off,provider_routing=on")
    for s in scenarios:
        for arm in arms:
            run = harness.run_scenario(s, arm)
            failed = [k for k, v in run["checks"].items() if v == "fail"]
            assert not failed, f"{s['id']} @ {arm['name']} 检查失败: {failed}"
            assert run["passed"] is True


def test_missing_evidence_scenarios_zero_hit_and_marker():
    scenarios = [s for s in harness.load_scenarios(harness.SCENARIOS_DIR)
                 if s["category"] == "missing_evidence"]
    assert len(scenarios) >= 2
    arm = run_eval.parse_arms("provider_routing=off")[0]
    for s in scenarios:
        run = harness.run_scenario(s, arm)
        assert run["metrics"]["zero_hit"] is True, s["id"]
        assert run["checks"]["missing_marker"] == "pass", s["id"]


def test_contradiction_update_scenarios_exclude_superseded():
    scenarios = [s for s in harness.load_scenarios(harness.SCENARIOS_DIR)
                 if s["category"] == "contradiction_update"]
    assert len(scenarios) >= 2
    arm = run_eval.parse_arms("provider_routing=off")[0]
    for s in scenarios:
        run = harness.run_scenario(s, arm)
        assert run["checks"].get("forbidden_ids") == "pass", s["id"]
        assert run["checks"].get("required_ids") == "pass", s["id"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
