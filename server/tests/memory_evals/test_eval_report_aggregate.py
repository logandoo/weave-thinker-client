"""TDD — W3 M4a 报告脚本：汇总 run_eval 指标 JSON → markdown。

技术规格源：docs/AUDIT_2026-10-13_deathmatch_memory_papers.md §3.4（M4a 报告脚本）
+ §10.6 W3 行。纯函数汇总（memory_eval_report.aggregate_eval_results /
render_eval_report）+ CLI；不连库。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/memory_evals -q
"""
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))

from scripts.memory_eval_report import (  # noqa: E402
    aggregate_eval_results,
    main as report_main,
    render_eval_report,
)


def _run(rid, cat, arm, hit_at_k, zero_hit, tokens, latency, llm="UNVERIFIED"):
    return {
        "id": rid, "category": cat, "arm": arm, "passed": True,
        "llm_verification": llm,
        "metrics": {"hit_at_k": hit_at_k, "zero_hit": zero_hit,
                    "injected_tokens": tokens, "latency_ms": latency,
                    "route": "off", "storage_rows": 2, "adoption_rate": None},
        "checks": {"required_ids": "pass"},
    }


def _results(runs):
    return {"meta": {"runner": "memory_evals.run_eval", "llm": "skipped",
                     "arms": ["provider_routing=off"]},
            "scenarios": runs, "summary": {}}


def test_aggregate_eval_results_by_category_and_arm():
    runs = [
        _run("s1", "cross_session_recall", "provider_routing=off", 1.0, False, 100, 1.5),
        _run("s2", "cross_session_recall", "provider_routing=on", 0.5, False, 90, 1.7),
        _run("s3", "missing_evidence", "provider_routing=off", None, True, 30, 0.9,
             llm="UNVERIFIED"),
    ]
    agg = aggregate_eval_results([_results(runs)])
    assert agg["total_runs"] == 3
    assert agg["llm_unverified"] == 3
    assert agg["by_category"]["cross_session_recall"]["runs"] == 2
    assert agg["by_category"]["missing_evidence"]["zero_hit_rate"] == 1.0
    assert agg["by_arm"]["provider_routing=on"]["mean_injected_tokens"] == 90
    # hit@k 均值跳过 None（无必中目标的场景不计入 hit@k，如缺证据类）
    assert abs(agg["by_arm"]["provider_routing=off"]["hit_at_k_mean"] - 1.0) < 1e-9


def test_render_eval_report_markdown():
    runs = [_run("s1", "temporal_list", "provider_routing=off", 1.0, False, 100, 1.5)]
    agg = aggregate_eval_results([_results(runs)])
    md = render_eval_report(agg)
    assert "记忆评测报告" in md
    assert "temporal_list" in md
    assert "provider_routing=off" in md
    assert "UNVERIFIED" in md
    assert "hit@k" in md and "零命中" in md and "注入 token" in md


def test_cli_eval_report_from_results_json(tmp_path):
    runs = [_run("s1", "preference_follow", "provider_routing=on", 1.0, False, 120, 2.0)]
    src = tmp_path / "latest.json"
    src.write_text(json.dumps(_results(runs), ensure_ascii=False), encoding="utf-8")
    out_md = tmp_path / "eval_report.md"
    rc = report_main(["--results", str(src), "--out", str(out_md)])
    assert rc == 0
    text = out_md.read_text(encoding="utf-8")
    assert "记忆评测报告" in text and "preference_follow" in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
