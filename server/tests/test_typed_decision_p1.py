"""P1 C2: typed judge failure flags + done probability floor (D-960 verdict side)."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.typed_decision import DEFAULT_OPTIONS, decide


def test_done_requires_probability_floor_and_margin():
    async def judge(prompt, loop):
        # Highest probability, but only 20% — the old utility argmax picked it.
        return {"probs": {"done": 0.2, "continue": 0.6, "wait": 0.1, "blocked": 0.05, "ask": 0.05}}

    d = asyncio.run(decide("q", DEFAULT_OPTIONS, judge=judge, max_loops=1,
                           done_min_prob=0.6, done_min_margin=0.2))
    assert d.choice == "continue"
    assert d.valid_distribution is True


def test_done_accepted_when_confident():
    async def judge(prompt, loop):
        return {"probs": {"done": 0.82, "continue": 0.10, "wait": 0.04, "blocked": 0.02, "ask": 0.02}}

    d = asyncio.run(decide("q", DEFAULT_OPTIONS, judge=judge, max_loops=1,
                           done_min_prob=0.6, done_min_margin=0.2))
    assert d.choice == "done"


def test_missing_judge_is_infra_failure_not_parse_failure():
    async def judge(prompt, loop):
        return None

    d = asyncio.run(decide("q", DEFAULT_OPTIONS, judge=judge, max_loops=1))
    assert d.choice == "continue"
    assert d.valid_distribution is False
    assert d.infra_failed is True
    assert d.parse_failed is False


def test_partial_probability_object_does_not_become_done():
    async def judge(prompt, loop):
        return {"probs": {"done": 0.05}}

    d = asyncio.run(decide("q", DEFAULT_OPTIONS, judge=judge, max_loops=1))
    assert d.choice == "continue"
    assert d.valid_distribution is False
    assert d.parse_failed is True


def test_last_loop_failure_is_not_masked_by_earlier_valid_loop():
    seq = [
        {"probs": {"done": 0.5, "continue": 0.4, "wait": 0.05, "blocked": 0.03, "ask": 0.02}},
        None,
    ]

    async def judge(prompt, loop):
        return seq[loop - 1]

    d = asyncio.run(decide("q", DEFAULT_OPTIONS, judge=judge, max_loops=2))
    assert d.choice == "continue"
    assert d.valid_distribution is False
    assert d.infra_failed is True


def test_invalid_json_shape_is_parse_failure():
    async def judge(prompt, loop):
        return {"probs": "not-a-dict"}

    d = asyncio.run(decide("q", DEFAULT_OPTIONS, judge=judge, max_loops=1))
    assert d.choice == "continue"
    assert d.valid_distribution is False
    assert d.parse_failed is True
    assert d.infra_failed is False


def test_typed_adapter_propagates_failure_flags(monkeypatch):
    from app.services import typed_decision as td

    async def fake_decide(*args, **kwargs):
        return td.TypedDecision(
            choice="continue", probs={"done": 0.1, "continue": 0.9},
            loops=1, confidence=0.9, valid_distribution=False,
            parse_failed=False, infra_failed=True,
        )

    monkeypatch.setattr(td, "decide", fake_decide)
    result = asyncio.run(td.typed_judge_verdict("goal", "response"))
    assert result is not None
    verdict, _reason, parse_failed, infra_failed, _compact, _tax = result
    assert verdict == "continue"
    assert parse_failed is False
    assert infra_failed is True
