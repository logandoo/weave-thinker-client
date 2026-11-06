"""P0 R3: per-turn memory read budget + parallel pending reservation."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.agent_loop import _maybe_dedupe_memory_read, _track_memory_read


def _state():
    return SimpleNamespace(
        memory_read_targets=set(),
        memory_read_pending=set(),
        memory_read_targets_all=set(),
        memory_read_budget=2,
    )


def test_same_target_in_flight_is_deduped_before_result():
    state = _state()
    assert _maybe_dedupe_memory_read(state, {"action": "read", "target": "agent"}) is None
    assert ("read", "agent") in state.memory_read_pending
    second = _maybe_dedupe_memory_read(state, {"action": "read", "target": "agent"})
    assert second is not None and json.loads(second)["deduplicated"] is True


def test_budget_allows_two_targets_and_rejects_third():
    state = _state()
    assert _maybe_dedupe_memory_read(state, {"action": "read", "target": "agent"}) is None
    assert _maybe_dedupe_memory_read(state, {"action": "read", "target": "user"}) is None
    third = _maybe_dedupe_memory_read(state, {"action": "read", "target": "system"})
    assert third is not None
    payload = json.loads(third)
    assert payload.get("budget_exceeded") is True
    assert "2" in payload.get("note", "")


def test_write_or_delegation_does_not_refund_budget():
    state = _state()
    assert _maybe_dedupe_memory_read(state, {"action": "read", "target": "agent"}) is None
    assert _maybe_dedupe_memory_read(state, {"action": "read", "target": "user"}) is None
    # A memory write invalidates the dedup cache for its target; a delegate
    # clears it entirely. Neither may refund the per-turn budget.
    state.memory_read_targets.clear()
    third = _maybe_dedupe_memory_read(state, {"action": "read", "target": "system"})
    assert third is not None and json.loads(third).get("budget_exceeded") is True


def test_failed_read_refunds_budget_for_retry():
    state = _state()
    _maybe_dedupe_memory_read(state, {"action": "read", "target": "agent"})
    _track_memory_read(state, {"action": "read", "target": "agent"}, '{"error": "timeout"}')
    assert ("read", "agent") not in state.memory_read_targets_all
    _maybe_dedupe_memory_read(state, {"action": "read", "target": "user"})
    _track_memory_read(state, {"action": "read", "target": "user"}, '{"error": "timeout"}')
    assert _maybe_dedupe_memory_read(state, {"action": "read", "target": "agent"}) is None


def test_budget_zero_blocks_all_reads():
    state = _state()
    state.memory_read_budget = 0
    result = _maybe_dedupe_memory_read(state, {"action": "read", "target": "agent"})
    assert result is not None and json.loads(result).get("budget_exceeded") is True


def test_success_moves_pending_to_completed_and_failed_read_does_not_poison():
    state = _state()
    _maybe_dedupe_memory_read(state, {"action": "read", "target": "agent"})
    _track_memory_read(state, {"action": "read", "target": "agent"}, '{"content": "ok"}')
    assert ("read", "agent") in state.memory_read_targets
    assert ("read", "agent") not in state.memory_read_pending

    state2 = _state()
    _maybe_dedupe_memory_read(state2, {"action": "read", "target": "user"})
    _track_memory_read(state2, {"action": "read", "target": "user"}, '{"error": "boom"}')
    assert ("read", "user") not in state2.memory_read_targets
    assert ("read", "user") not in state2.memory_read_pending
