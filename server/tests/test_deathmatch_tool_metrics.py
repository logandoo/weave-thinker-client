"""TDD — 遗留清偿 B4：重复工具调用/工具错误度量（D-20 部分落实）
（论文漏采 B3/B4 补入 D3 统计清单；报告 §10 复核）。

数据源：归档会话的 messages.tool_calls / tool_results（JSON 文本列）；
离线纯函数统计，进 harness report 的 B4 节供蒸馏器/人工裁定。

fix-round 裁决（B4 生产形状）：`_load` 改展平器——输入可为 str（json.loads）/
list（元素逐个：str→loads、dict→直用）；tool_results 的 {"agent_steps": [...]}
包装剥开；错误识别=条目 error/is_error 键**或**条目 result（JSON 串/字典）内含
"error": true。`repeated_calls` 改冗余口径 sum(n-1 for n>=2) 并更名 redundant_calls。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_deathmatch_tool_metrics.py -v
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.deathmatch_harness_report import (
    aggregate_harness_runs,
    compute_tool_stats,
    render_report,
)


def test_tool_stats_counts_redundant_and_errors():
    calls = json.dumps([
        {"id": "1", "name": "workspace_read", "arguments": "{\"path\":\"a.md\"}"},
        {"id": "2", "name": "workspace_read", "arguments": "{\"path\":\"a.md\"}"},  # 冗余
        {"id": "3", "name": "workspace_read", "arguments": "{\"path\":\"a.md\"}"},  # 冗余
        {"id": "4", "name": "execute_code", "arguments": "{\"code\":\"x\"}"},
    ])
    results = json.dumps([
        {"tool_call_id": "1", "content": "ok"},
        {"tool_call_id": "2", "content": "ok"},
        {"tool_call_id": "3", "content": "boom", "error": True},
        {"tool_call_id": "4", "content": "err", "error": True},
    ])
    stats = compute_tool_stats(calls, results)
    assert stats["total_calls"] == 4
    assert stats["redundant_calls"] == 2, "冗余口径 sum(n-1 for n>=2)：同签名 3 次 → 2 条冗余"
    assert stats["error_results"] == 2
    assert stats["top_repeated"], "须给出重复签名清单"
    assert any(t["name"] == "workspace_read" and t["count"] == 3 for t in stats["top_repeated"])


def test_tool_stats_tolerant_shapes():
    assert compute_tool_stats(None, None)["total_calls"] == 0
    assert compute_tool_stats("not-json", "[]")["total_calls"] == 0
    s = compute_tool_stats(json.dumps([{"name": "t", "arguments": {"a": 1}}]), "[]")
    assert s["total_calls"] == 1


def test_aggregate_and_report_include_tool_metrics():
    rows = [
        {"final_status": "done", "turns": 3, "verify_failures": 0, "issues": [],
         "config_json": "{}", "events": [],
         "tool_stats": compute_tool_stats(
             json.dumps([{"name": "t", "arguments": "{}"}, {"name": "t", "arguments": "{}"}]),
             json.dumps([{"tool_call_id": "1", "error": True}]))},
    ]
    agg = aggregate_harness_runs(rows)
    tm = agg.get("tool_metrics") or {}
    assert tm.get("total_calls") == 2
    assert tm.get("redundant_calls") == 1, "同签名 2 次 → 1 条冗余"
    assert tm.get("error_results") == 1
    report = render_report(agg, days=30)
    assert "重复工具调用" in report or "工具错误" in report


# ── B4 fix-round：三类生产形状（喂真实形状，禁空真） ───────────────────────

def test_tool_stats_per_message_json_string_list():
    """生产形状 ①：_load_rows 逐消息攒出的 list[str|None]——每元素是该消息
    tool_calls/tool_results 列的 JSON 文本（或 None）。"""
    calls = [
        json.dumps([{"id": "1", "name": "workspace_read", "arguments": "{\"path\":\"a.md\"}"}]),
        None,
        json.dumps([
            {"id": "2", "name": "workspace_read", "arguments": "{\"path\":\"a.md\"}"},
            {"id": "3", "name": "execute_code", "arguments": "{\"code\":\"x\"}"},
        ]),
    ]
    results = [
        json.dumps([{"tool_call_id": "1", "content": "ok"}]),
        None,
        json.dumps([
            {"tool_call_id": "2", "content": "ok"},
            {"tool_call_id": "3", "content": "boom", "error": True},
        ]),
    ]
    stats = compute_tool_stats(calls, results)
    assert stats["total_calls"] == 3, "per-message JSON 字符串列表必须展平计数"
    assert stats["redundant_calls"] == 1
    assert stats["error_results"] == 1


def test_tool_stats_agent_steps_wrapper_unwrapped():
    """生产形状 ②：messages.tool_results 的 {"agent_steps": [...]} 包装剥开后
    逐条识别（error/is_error 键）。"""
    calls = [json.dumps([
        {"id": "1", "name": "terminal", "arguments": "{}"},
        {"id": "2", "name": "terminal", "arguments": "{}"},
    ])]
    results = [json.dumps({
        "rounds": [], "results": [], "search_failed": False,
        "agent_steps": [
            {"name": "1", "title": "终端命令", "content": "ok", "step_type": "tool"},
            {"name": "2", "title": "终端命令", "content": "boom", "step_type": "tool",
             "is_error": True},
        ],
    })]
    stats = compute_tool_stats(calls, results)
    assert stats["total_calls"] == 2
    assert stats["error_results"] == 1, "agent_steps 包装内的 is_error 条目必须计错误"


def test_tool_stats_nested_result_error():
    """生产形状 ③：条目 result（JSON 串/字典）内含 "error": true 也算错误。"""
    results = [json.dumps([
        {"name": "web_search", "result": "{\"error\": true, \"message\": \"quota\"}"},
        {"name": "web_search", "result": {"error": True}},
        {"name": "web_search", "result": "{\"hits\": []}"},
        {"name": "web_search", "is_error": False, "result": "plain text"},
    ])]
    stats = compute_tool_stats(None, results)
    assert stats["error_results"] == 2, "嵌套 result 错误（串/字典）必须识别；正常结果不误计"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
