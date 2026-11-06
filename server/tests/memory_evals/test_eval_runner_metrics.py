"""TDD — W3 M4a 离线记忆评测台 runner/指标层（MemEvolve 2512.18746 评测台采纳件）。

技术规格源：docs/AUDIT_2026-10-13_deathmatch_memory_papers.md §3.4 改进设计（M4a）
+ §10.4 E4（租户隔离：独立测试用户/独立库/离线行源——**绝不连生产库**）。

runner（tests/memory_evals/run_eval.py，metadata-only 风格）：场景 JSON + 假检索
结果注入 → 指标（hit@k、零命中率、注入 token、延迟；采纳率/存储行数可选）；
LLM 路径缺省跳过并标 UNVERIFIED。不依赖真实 LLM/DB/网络。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/memory_evals -q
"""
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))
sys.path.insert(0, _HERE)

import harness  # noqa: E402  tests/memory_evals/harness.py
import run_eval  # noqa: E402  tests/memory_evals/run_eval.py


def _scenario(sid: str, category: str, query: str, candidates: list,
              hit_ids=None, **expect_kw) -> dict:
    return {
        "id": sid,
        "category": category,
        "title": sid,
        "query": query,
        "stage0": {"query_type": "semantic", "keywords": ["kw"]},
        "seed": {"concepts": [], "episodic": [], "subconscious": []},
        "fake_retrieval": {"candidates": candidates},
        "expect": {"hit_ids": hit_ids or [], "hit_k": 3, **expect_kw},
    }


_CAND_PORT = {"id": "c-port", "tier": "concept", "score": 0.82,
              "content": "部署端口是 8158",
              "metadata": {"canonical_name": "部署端口"}}
_CAND_EPI = {"id": "e-meet", "tier": "episodic", "score": 0.55,
             "content": "[2026-10-01] 开会定了部署方案", "metadata": {}}


# ── runner 产出指标 JSON（2 个合成场景，零 LLM） ───────────────────────────

def test_runner_two_synthetic_scenarios_produce_metrics_json(tmp_path):
    scen_dir = tmp_path / "scenarios"
    scen_dir.mkdir()
    s1 = _scenario("synth_hit_01", "cross_session_recall", "我上次说的部署端口是多少？",
                   [_CAND_PORT, _CAND_EPI], hit_ids=["c-port"],
                   llm_checks=[{"type": "answer_contains", "value": "8158"}])
    s2 = _scenario("synth_miss_01", "missing_evidence", "上次说的那本书叫什么来着？",
                   [], want_zero_hit=True, expect_missing_marker=True,
                   llm_checks=[{"type": "honest_missing", "value": "admit"}])
    for s in (s1, s2):
        (scen_dir / f"{s['id']}.json").write_text(
            json.dumps(s, ensure_ascii=False), encoding="utf-8")

    out_json = tmp_path / "results.json"
    rc = run_eval.main([
        "--scenarios-dir", str(scen_dir), "--out", str(out_json),
        "--arms", "provider_routing=off",
    ])
    assert rc == 0
    results = json.loads(out_json.read_text(encoding="utf-8"))
    runs = results["scenarios"]
    assert len(runs) == 2, "2 个合成场景 × 1 臂 = 2 条指标记录"

    by_id = {r["id"]: r for r in runs}
    hit_run = by_id["synth_hit_01"]
    for key in ("hit_at_k", "zero_hit", "injected_tokens", "latency_ms"):
        assert key in hit_run["metrics"], f"指标 JSON 缺 {key}"
    assert hit_run["metrics"]["hit_at_k"] == 1.0
    assert hit_run["metrics"]["zero_hit"] is False
    assert hit_run["metrics"]["injected_tokens"] > 0
    assert hit_run["metrics"]["latency_ms"] >= 0
    # LLM 路径缺省跳过 → UNVERIFIED
    assert hit_run["llm_verification"] == "UNVERIFIED"

    miss_run = by_id["synth_miss_01"]
    assert miss_run["metrics"]["zero_hit"] is True
    assert miss_run["checks"]["missing_marker"] == "pass"
    assert miss_run["llm_verification"] == "UNVERIFIED"


def test_runner_llm_path_skipped_and_marked_unverified():
    s = _scenario("synth_llm", "preference_follow", "按我的偏好推荐编辑器",
                  [_CAND_PORT], hit_ids=["c-port"],
                  llm_checks=[{"type": "answer_follows_preference", "value": "vim"}])
    run = harness.run_scenario(s, run_eval.parse_arms("provider_routing=off")[0])
    assert run["llm_verification"] == "UNVERIFIED"
    assert run["llm_path"] == "skipped"


def test_runner_no_llm_checks_marked_not_required():
    s = _scenario("synth_nollm", "cross_session_recall", "上次的端口", [_CAND_PORT],
                  hit_ids=["c-port"])
    run = harness.run_scenario(s, run_eval.parse_arms("provider_routing=off")[0])
    assert run["llm_verification"] == "NOT_REQUIRED"


# ── 指标层纯函数 ──────────────────────────────────────────────────────────

def test_metrics_hit_at_k_partial():
    s = _scenario("synth_partial", "cross_session_recall", "q",
                  [_CAND_PORT, _CAND_EPI], hit_ids=["c-port", "e-miss"], hit_k=1)
    run = harness.run_scenario(s, run_eval.parse_arms("provider_routing=off")[0])
    assert run["metrics"]["hit_at_k"] == 0.5
    assert run["metrics"]["hit_ids"] == ["c-port"]
    assert run["metrics"]["missed_ids"] == ["e-miss"]


def test_metrics_zero_hit_and_optional_adoption():
    s = _scenario("synth_zero", "missing_evidence", "上次提过的书", [],
                  want_zero_hit=True, expect_missing_marker=True)
    run = harness.run_scenario(s, run_eval.parse_arms("provider_routing=off")[0])
    assert run["metrics"]["zero_hit"] is True
    assert run["metrics"]["adoption_rate"] is None

    s2 = _scenario("synth_adopt", "cross_session_recall", "上次的端口", [_CAND_PORT],
                   hit_ids=["c-port"], adopted_ids=["c-port"])
    run2 = harness.run_scenario(s2, run_eval.parse_arms("provider_routing=off")[0])
    assert run2["metrics"]["adoption_rate"] == 1.0


def test_metrics_storage_rows_counted_from_seed():
    s = _scenario("synth_rows", "cross_session_recall", "上次的端口", [_CAND_PORT],
                  hit_ids=["c-port"])
    s["seed"] = {"concepts": [{"id": "c1", "content": "x"}],
                 "episodic": [{"id": "e1"}, {"id": "e2"}], "subconscious": []}
    run = harness.run_scenario(s, run_eval.parse_arms("provider_routing=off")[0])
    assert run["metrics"]["storage_rows"] == 3


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
