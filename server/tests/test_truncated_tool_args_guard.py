"""TDD tests — agent/chat 路径的截断工具参数守卫（2026-09-21 波 2-A）。

事故同类（conv fcb2e60a）在普通 Agent 路径的镜像：args 不可解析时
`repair → 仍失败 → {}` 后照常派发（agent_loop.py 旧行为），工具会以空参数
执行（notes 回 "Unknown action"）。本守卫复用 voice 波同一分类语义：
不可解析/非对象的参数一律不派发，回可行动的截断错误。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_truncated_tool_args_guard.py -v
"""
import json
from pathlib import Path

from app.services.retry_utils import (
    decide_tool_dispatch,
    parse_tool_arguments,
    plan_tool_call_for_dispatch,
    plan_tool_calls_for_round,
    truncated_tool_args_feedback,
)

FIXTURE = Path(__file__).parent / "fixtures" / "voice_truncated_create_note_args.txt"


def _tc(name: str, args: str, call_id: str = "call_1") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


class TestParseToolArguments:
    def test_valid_object_roundtrips(self):
        assert parse_tool_arguments('{"action": "list_notes"}') == {"action": "list_notes"}

    def test_empty_and_none_are_empty_dict(self):
        assert parse_tool_arguments("") == {}
        assert parse_tool_arguments(None) == {}
        assert parse_tool_arguments("   ") == {}

    def test_production_truncated_payload_is_unparseable(self):
        raw = FIXTURE.read_text(encoding="utf-8")
        assert parse_tool_arguments(raw) is None

    def test_non_object_json_is_invalid(self):
        assert parse_tool_arguments("[1, 2]") is None
        assert parse_tool_arguments('"just a string"') is None
        assert parse_tool_arguments("123") is None

    def test_missing_closer_after_complete_string_is_repaired(self):
        # cut after a COMPLETE value — only braces are missing → safe to repair
        assert parse_tool_arguments('{"action": "list_notebooks"') == {"action": "list_notebooks"}


class TestPlanToolCallForDispatch:
    def test_valid_call_returns_args(self):
        args, error = plan_tool_call_for_dispatch(_tc("notes", '{"action": "list_notes"}'))
        assert args == {"action": "list_notes"} and error is None

    def test_truncated_call_returns_feedback_and_no_args(self):
        raw = FIXTURE.read_text(encoding="utf-8")
        args, error = plan_tool_call_for_dispatch(_tc("notes", raw))
        assert args is None
        assert error is not None
        data = json.loads(error)
        assert data["_truncated"] is True
        assert "截断" in data["error"] and "分块" in data["error"]


class TestTruncatedFeedback:
    def test_feedback_mentions_tool_and_chunking(self):
        data = json.loads(truncated_tool_args_feedback("workspace_write"))
        assert data["_truncated"] is True
        assert "workspace_write" in data["error"]
        assert "分块" in data["error"]


class TestPlanToolCallsForRound:
    """A4.9 C1 regression: planning MUST run on raw args and NEVER mutate them.

    The old ordering normalized unparseable args to the `"{}"` sentinel
    in place first, so the guard never saw the truncated payload and the call
    was dispatched with empty arguments.
    """

    def test_truncated_round_plans_feedback_and_preserves_originals(self):
        raw = FIXTURE.read_text(encoding="utf-8")
        tc = _tc("notes", raw, call_id="call_trunc")
        plan, safe_calls = plan_tool_calls_for_round([tc])

        args, error = plan["call_trunc"]
        assert args is None, "truncated call must not be dispatchable"
        assert error is not None and json.loads(error)["_truncated"] is True
        # provider-safe copy is valid JSON …
        assert safe_calls[0]["function"]["arguments"] == "{}"
        assert json.loads(safe_calls[0]["function"]["arguments"]) == {}
        # … while the original call keeps the raw truncated payload (forensics)
        assert tc["function"]["arguments"] == raw

    def test_mixed_round_classifies_independently(self):
        raw = FIXTURE.read_text(encoding="utf-8")
        calls = [
            _tc("notes", '{"action": "list_notebooks"}', call_id="call_ok"),
            _tc("notes", raw, call_id="call_trunc"),
        ]
        plan, safe_calls = plan_tool_calls_for_round(calls)
        assert plan["call_ok"] == ({"action": "list_notebooks"}, None)
        assert plan["call_trunc"][1] is not None and plan["call_trunc"][0] is None
        assert safe_calls[0]["function"]["arguments"] == '{"action": "list_notebooks"}'
        assert safe_calls[1]["function"]["arguments"] == "{}"

    def test_non_string_arguments_fail_closed(self):
        assert parse_tool_arguments({"action": "x"}) is None
        tc = {"id": "c1", "type": "function", "function": {"name": "notes", "arguments": {"action": "x"}}}
        args, error = plan_tool_call_for_dispatch(tc)
        assert args is None and error is not None


class TestAgentLoopIntegration:
    """A4.9 r2 regression: the agent path must actually reach the guard.

    Catches the class of defect where the guard exists but the loop never uses
    it (r1: dead code because normalization ran first; r2: missing import).
    """

    def test_agent_loop_binds_round_planner_and_decider(self):
        from app.services import agent_loop as al

        # module-level binding — a missing/renamed import fails here
        assert al.plan_tool_calls_for_round is plan_tool_calls_for_round
        assert al.decide_tool_dispatch is decide_tool_dispatch

    def test_agent_dispatch_decision_never_reaches_registry_for_fixture(self, monkeypatch):
        """Execute the exact per-call decision the two dispatch sites use."""
        from app.services import agent_loop as al
        from app.tools.registry import registry

        raw = FIXTURE.read_text(encoding="utf-8")
        tc = _tc("notes", raw, call_id="call_trunc")

        # per-round planning exactly as the loop does it (raw args, before any
        # provider normalization), then the site's decision lookup
        plan, safe_calls = al.plan_tool_calls_for_round([tc])

        dispatched = []

        def _spy_dispatch(*args, **kwargs):
            dispatched.append(args)
            raise AssertionError("truncated call must never be dispatched")

        monkeypatch.setattr(registry, "dispatch", _spy_dispatch)

        args, error = al.decide_tool_dispatch(plan, tc)
        if error is None:  # the sites dispatch only when error is None
            registry.dispatch(tc["function"]["name"], args)

        assert error is not None, "truncated fixture must produce a fail-closed decision"
        assert json.loads(error)["_truncated"] is True
        assert dispatched == []
        # provider-bound copy is valid JSON while the original stays raw
        assert safe_calls[0]["function"]["arguments"] == "{}"
        assert tc["function"]["arguments"] == raw
