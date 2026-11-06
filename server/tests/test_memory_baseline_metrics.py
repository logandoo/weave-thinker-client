"""W0-1/W2-3/W2-4（记忆/长程 v2，2026-10-23，判据 #2/#13/#14）：基线度量
（tokens/session · cache_hit_ratio · p95 · 注入字符 · 重复工具率 · done→reopen
率，按模型分档）+ 读时门槛误杀 shadow 可观测。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_memory_baseline_metrics.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestAggregation:
    def test_p95(self):
        from app.services.memory_baseline_service import p95

        assert p95([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == pytest.approx(9.55, abs=0.1)
        assert p95([]) == 0.0

    def test_aggregate_recall_rows(self):
        from app.services.memory_baseline_service import aggregate_recall_rows

        rows = [
            {"cache_hit": True, "elapsed_ms": 10, "injected_chars": 100},
            {"cache_hit": False, "elapsed_ms": 30, "injected_chars": 300},
        ]
        out = aggregate_recall_rows(rows)
        assert out["cache_hit_ratio"] == pytest.approx(0.5)
        # 线性插值口径（p95 docstring 钉死）：[10,30] → 29.0
        assert out["p95_latency_ms"] >= 29
        assert out["injected_chars_avg"] == 200

    def test_duplicate_tool_rate(self):
        from app.services.memory_baseline_service import duplicate_tool_rate

        seqs = [["web_search:a", "web_search:a", "memory_read:b"]]
        out = duplicate_tool_rate(seqs)
        assert out["tool_calls"] == 3
        assert out["duplicate_rate"] == pytest.approx(1 / 3)

    def test_reopen_rate(self):
        from app.services.memory_baseline_service import reopen_rate

        out = reopen_rate([{"done": True, "later_user_turns": 2},
                           {"done": True, "later_user_turns": 0}])
        assert out["done_runs"] == 2
        assert out["reopen_rate"] == pytest.approx(0.5)


class TestGateRejectShadow:
    """#13 W2-3：injection_min_relevance 拒绝面必须可观测（false-quarantine）。"""

    def test_quarantine_stats(self):
        from app.services.memory_baseline_service import quarantine_stats

        rejects = [
            {"id": "c1", "tier": "concept", "score": 0.21, "floor": 0.35},
            {"id": "c2", "tier": "concept", "score": 0.30, "floor": 0.35},
            {"id": "e1", "tier": "episodic", "score": 0.1, "floor": 0.30},
        ]
        out = quarantine_stats(rejects, injected_count=7)
        assert out["rejected"] == 3
        assert out["by_tier"]["concept"] == 2
        assert out["reject_rate"] == pytest.approx(3 / 10)

    def test_empty(self):
        from app.services.memory_baseline_service import quarantine_stats

        assert quarantine_stats([], injected_count=5)["rejected"] == 0


class TestModelTieringInReport:
    def test_group_by_model(self):
        from app.services.memory_baseline_service import group_by_model

        rows = [
            {"model": "m1", "prompt_tokens": 10, "completion_tokens": 5},
            {"model": "m1", "prompt_tokens": 20, "completion_tokens": 5},
            {"model": "m2", "prompt_tokens": 1, "completion_tokens": 1},
        ]
        out = group_by_model(rows)
        assert out["m1"]["tokens"] == 40
        assert out["m2"]["tokens"] == 2


class TestBaselineReport:
    def test_render_report_sections(self):
        from app.services.memory_baseline_service import render_baseline_report

        md = render_baseline_report({
            "window_days": 7,
            "by_model": {"m1": {"tokens": 40, "sessions": 2}},
            "recall": {"cache_hit_ratio": 0.5, "p95_latency_ms": 30.0,
                       "injected_chars_avg": 200},
            "tools": {"tool_calls": 3, "duplicate_rate": 0.33},
            "reopen": {"done_runs": 2, "reopen_rate": 0.5},
            "quarantine": {"rejected": 3, "reject_rate": 0.3},
            "alerts": ["误杀率 0.30 超过 0.10 阈值"],
        })
        assert "cache_hit_ratio" in md
        assert "p95" in md
        assert "reopen" in md
        assert "告警" in md
