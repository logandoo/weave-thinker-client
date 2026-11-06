"""J1: typed looped decision (2026-10-18)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.typed_decision import (
    DEFAULT_OPTIONS,
    brier_score,
    decide,
)


def test_early_stop_after_confident_first_loop():
    calls = []

    async def judge(prompt, loop):
        calls.append(loop)
        return {"probs": {"done": 0.95, "continue": 0.02, "wait": 0.01,
                          "blocked": 0.01, "ask": 0.01}}

    d = asyncio.run(decide("q", DEFAULT_OPTIONS, judge=judge))
    assert d.loops == 1
    assert d.choice == "done"
    assert len(calls) == 1


def test_uncertain_walks_three_loops():
    seq = [
        {"probs": {"done": 0.4, "continue": 0.3, "wait": 0.1, "blocked": 0.1, "ask": 0.1}},
        {"probs": {"done": 0.25, "continue": 0.45, "wait": 0.1, "blocked": 0.1, "ask": 0.1}},
        {"probs": {"done": 0.5, "continue": 0.25, "wait": 0.1, "blocked": 0.1, "ask": 0.05}},
    ]
    seen = []

    async def judge(prompt, loop):
        seen.append(loop)
        return seq[min(loop - 1, len(seq) - 1)]

    d = asyncio.run(decide("q", DEFAULT_OPTIONS, judge=judge, max_loops=3))
    assert d.loops == 3
    assert len(seen) == 3
    # P1 C2: 0.5 done with only 0.25 margin is below the safe done floor.
    assert d.choice == "continue"


def test_utility_asymmetry_can_prefer_continue():
    # Probability of done is highest, but utility-weighted continue wins when
    # the done utility is deliberately high-cost (negative).
    async def judge(prompt, loop):
        return {"probs": {"done": 0.55, "continue": 0.45, "wait": 0.0,
                          "blocked": 0.0, "ask": 0.0}}

    d = asyncio.run(decide(
        "q", DEFAULT_OPTIONS,
        utilities={"done": -1.0, "continue": 0.25, "wait": 0.1,
                   "blocked": 0.1, "ask": 0.1},
        judge=judge, max_loops=1,
    ))
    assert d.choice == "continue"


def test_brier_score_bounds():
    assert brier_score({"done": 1.0, "continue": 0.0}, "done") == 0.0
    assert brier_score({"done": 0.5, "continue": 0.5}, "done") == 0.5


def test_expected_calibration_error():
    from app.services.typed_decision import expected_calibration_error
    # Perfectly confident and correct -> 0.
    assert expected_calibration_error([
        {"probs": {"done": 1.0}, "outcome": "done"},
    ]) == 0.0
    # Confident but wrong -> gap 1.0.
    assert expected_calibration_error([
        {"probs": {"done": 1.0}, "outcome": "continue"},
    ]) == 1.0
    # Top-label confidence 0.5, accuracy 1.0 in its bin -> gap 0.5.
    assert expected_calibration_error([
        {"probs": {"done": 0.5, "continue": 0.5}, "outcome": "done"},
    ]) == 0.5


def test_judge_failure_defaults_to_continue_not_done():
    async def judge(prompt, loop):
        return None

    d = asyncio.run(decide("q", DEFAULT_OPTIONS, judge=judge, max_loops=1))
    assert d.choice == "continue"
    assert d.confidence <= 0.5
