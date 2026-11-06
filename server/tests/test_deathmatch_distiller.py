"""TDD — W1 D3 蒸馏器统计面（论文 2609.26261 CASD 采纳件：离线工具，人工门）
（docs/AUDIT_2026-10-13_deathmatch_memory_papers.md §4.2/§10）。

死亡模式语料统计扩展（B4 修正方式/事件类型分布）+ LLM 规则蒸馏提案
（docs/proposals/，绝不自动改 harness；LLM 失败 fail-open 落统计清单）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_deathmatch_distiller.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.deathmatch_harness_report import (
    aggregate_harness_runs,
    render_distill_proposal,
    render_report,
)


def _rows():
    return [
        {
            "final_status": "done", "turns": 6, "verify_failures": 1,
            "issues": ["文件未生成", "文件未生成", "字数超范围"],
            "config_json": "{}",
            "events": [
                {"type": "node_recovery", "action": "local_patch"},
                {"type": "node_recovery", "action": "local_retry"},
                {"type": "node_recovery", "action": "local_patch"},
                {"type": "evidence_gate_block", "gate_blocks": 1},
                {"type": "consensus_challenge"},
            ],
        },
        {
            "final_status": "partial_complete", "turns": 12, "verify_failures": 3,
            "issues": ["文件未生成"],
            "config_json": "{}",
            "events": [{"type": "node_recovery", "action": "replan"}],
        },
    ]


def test_aggregate_counts_correction_mix_and_event_types():
    agg = aggregate_harness_runs(_rows())
    assert agg["correction_mix"].get("local_patch") == 2
    assert agg["correction_mix"].get("local_retry") == 1
    assert agg["correction_mix"].get("replan") == 1
    assert agg["event_type_counts"].get("evidence_gate_block") == 1
    assert agg["event_type_counts"].get("consensus_challenge") == 1


def test_aggregate_flags_recurrent_issue_classes():
    agg = aggregate_harness_runs(_rows())
    top = agg["issue_recurrence"][0]
    assert top["issue_class"] == "文件未生成"
    assert top["count"] == 3
    assert top["harness_candidate"] is True, "≥3 次复发须标 harness 候选"


def test_render_report_includes_d3_sections():
    report = render_report(aggregate_harness_runs(_rows()), days=30)
    assert "修正方式分布" in report
    assert "local_patch: 2" in report
    assert "事件类型分布" in report


def test_render_distill_proposal_stats_only_fallback():
    doc = render_distill_proposal(aggregate_harness_runs(_rows()), [], days=30)
    assert "人工门" in doc
    assert "统计清单" in doc, "LLM 不可用时必须落统计清单（fail-open）"
    assert "文件未生成" in doc


def test_render_distill_proposal_with_rules():
    rules = [{"pattern": "文件未生成 ×3", "proposed_rule": "步骤契约须列声明产出",
              "evidence": "issues 复发 3 次"}]
    doc = render_distill_proposal(aggregate_harness_runs(_rows()), rules, days=30)
    assert "步骤契约须列声明产出" in doc
    assert "勿直接改 harness" in doc


def test_unpack_rows_reads_real_events_shape():
    """C1（复审）：_load_rows 的拆包缝必须吃下 join 取得的 events_json
    （生产形状=conversations.deathmatch_events 文本），杜绝伪造键假绿。"""
    import json as _json
    from scripts.deathmatch_harness_report import _unpack_rows

    rows = [{
        "conversation_id": "c1", "final_status": "done", "turns": 3,
        "issues_json": _json.dumps(["文件未生成"]),
        "events_json": _json.dumps([
            {"type": "node_recovery", "action": "local_patch", "correction_kind": "local_patch"},
        ]),
    }]
    out = _unpack_rows(rows)
    assert out[0]["issues"] == ["文件未生成"]
    assert out[0]["events"][0]["type"] == "node_recovery"
    agg = aggregate_harness_runs(out)
    assert agg["correction_mix"].get("local_patch") == 1
    assert agg["event_type_counts"].get("node_recovery") == 1


def test_unpack_rows_handles_asyncpg_decoded_list_shape():
    """NEW-1（复审实证）：asyncpg 对 JSON 列自动解码为 Python list——
    拆包缝必须双形态兼容（list 直通 / str 才 loads），否则生产 events 恒空。"""
    from scripts.deathmatch_harness_report import _unpack_rows

    rows = [{
        "conversation_id": "c1", "final_status": "done", "turns": 3,
        "issues_json": '["文件未生成"]',
        "events_json": [  # ← 生产形状（已解码 list），不是 JSON 字符串
            {"type": "node_recovery", "action": "local_patch", "correction_kind": "local_patch"},
            {"type": "evidence_gate_block", "gate_blocks": 2},
        ],
    }]
    out = _unpack_rows(rows)
    assert out[0]["events"], "已解码 list 形状不得被吞（生产 events 轴不得恒空）"
    agg = aggregate_harness_runs(out)
    assert agg["correction_mix"].get("local_patch") == 1
    assert agg["event_type_counts"].get("evidence_gate_block") == 1
