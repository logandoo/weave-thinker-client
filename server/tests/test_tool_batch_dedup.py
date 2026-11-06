"""P0 R3: duplicate tool calls in one assistant round are planned once.

The production incident had 6 web_search calls = 3 unique query sets x2 and
8 memory reads = 4 targets x2, all in the same parallel wave.  Per-call
dedup cannot see siblings (they all check before any result is tracked), so
the batch planner must collapse identical signatures before dispatch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.retry_utils import plan_tool_calls_for_round


def _tc(call_id: str, name: str, args: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def test_duplicate_web_search_queries_are_planned_once():
    calls = [
        _tc("c1", "web_search", {"queries": ["AMD EPYC", "Intel Xeon"]}),
        _tc("c2", "web_search", {"queries": ["intel xeon ", "amd epyc"]}),
        _tc("c3", "web_search", {"queries": ["AMD EPYC"]}),
    ]
    plan, safe = plan_tool_calls_for_round(calls)
    assert len(safe) == 3
    assert plan["c1"][0] is not None
    assert plan["c2"][0] is None and "duplicate" in (plan["c2"][1] or "").lower()
    assert plan["c3"][0] is not None


def test_duplicate_memory_read_is_planned_once_but_write_is_not_deduped():
    calls = [
        _tc("m1", "memory", {"action": "read", "target": "user"}),
        _tc("m2", "memory", {"action": "read", "target": "user"}),
        _tc("m3", "memory", {"action": "add", "target": "user", "content": "x"}),
        _tc("m4", "memory", {"action": "add", "target": "user", "content": "x"}),
    ]
    plan, _ = plan_tool_calls_for_round(calls)
    assert plan["m1"][0] is not None
    assert plan["m2"][0] is None
    assert plan["m3"][0] is not None
    assert plan["m4"][0] is not None


def test_other_duplicate_tools_are_not_batch_deduped():
    calls = [
        _tc("a1", "notes", {"action": "list"}),
        _tc("a2", "notes", {"action": "list"}),
    ]
    plan, _ = plan_tool_calls_for_round(calls)
    assert plan["a1"][0] is not None
    assert plan["a2"][0] is not None


def test_incident_shape_six_searches_three_unique():
    calls = []
    for i, q in enumerate(["AMD EPYC", "Intel Xeon", "ARM server"] * 2, 1):
        calls.append(_tc(f"w{i}", "web_search", {"queries": [q]}))
    plan, _ = plan_tool_calls_for_round(calls)
    dispatched = [cid for cid, (args, _err) in plan.items() if args is not None]
    assert len(dispatched) == 3
    assert len(plan) == 6


def test_incident_shape_eight_memory_reads_four_unique():
    calls = []
    for i, target in enumerate(["agent", "user", "system", "shared"] * 2, 1):
        calls.append(_tc(f"m{i}", "memory", {"action": "read", "target": target}))
    plan, _ = plan_tool_calls_for_round(calls)
    dispatched = [cid for cid, (args, _err) in plan.items() if args is not None]
    # Batch dedup collapses the exact duplicate targets; budget enforcement
    # then caps the distinct targets at 2 in the execution layer.
    assert len(dispatched) == 4
    assert len(plan) == 8
