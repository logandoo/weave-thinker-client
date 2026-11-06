"""T1b: tool reliability ledger + stable re-rank (2026-10-18)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import tool_reliability_service as trs


def setup_function():
    trs.reset_for_tests()


def test_cold_start_is_neutral_and_stable():
    cands = [(3.0, {"name": "a", "server": "s"}), (3.0, {"name": "b", "server": "s"})]
    ranked = trs.rank_candidates(cands)
    assert [c["name"] for _s, c in ranked] == ["a", "b"]


def test_failures_lower_rank_and_successes_raise():
    trs.record("bad", ok=False, error_class="timeout", latency_ms=5000)
    trs.record("bad", ok=False, error_class="timeout", latency_ms=5000)
    trs.record("good", ok=True, latency_ms=10)
    trs.record("good", ok=True, latency_ms=10)
    cands = [(1.0, {"name": "bad", "server": "s"}), (1.0, {"name": "good", "server": "s"})]
    ranked = trs.rank_candidates(cands)
    assert [c["name"] for _s, c in ranked] == ["good", "bad"]


def test_provider_order_keeps_primary_first():
    trs.record("web_search:exa", ok=False, error_class="timeout")
    trs.record("web_search:exa", ok=False, error_class="timeout")
    order = trs.provider_order(["deepseek", "exa", "tavily", "bing"], "deepseek")
    assert order[0] == "deepseek"
    assert set(order[1:]) == {"exa", "tavily", "bing"}
