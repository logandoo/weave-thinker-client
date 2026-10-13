"""TDD tests — voice tool-call argument truncation handling (2026-09-21).

生产 conv fcb2e60a：语音 max_tokens=1024 截断 create_note 的 arguments JSON，
voice_service 把不可解析的参数静默降级为 {}，notes 工具回 "Unknown action"，
模型同构重试 7 次后放弃。修复：截断调用不派发（不执行残缺参数）、向模型回
可行动的截断错误（分块写入），并在轮内把输出预算翻倍（有上限/有界重试）。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_voice_tool_arg_truncation.py -v
"""
import json
from pathlib import Path

from app.services.voice_service import (
    _escalated_voice_max_tokens,
    _plan_voice_tool_calls,
    _voice_truncation_feedback,
)

FIXTURE = Path(__file__).parent / "fixtures" / "voice_truncated_create_note_args.txt"


def _tc(name: str, args: str) -> dict:
    return {
        "id": f"call_{name}_1",
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


class TestPlanVoiceToolCalls:
    def test_valid_call_is_dispatchable(self):
        raw = '{"action": "list_notebooks"}'
        plan = _plan_voice_tool_calls([_tc("notes", raw)])
        assert len(plan) == 1
        assert plan[0]["truncated"] is False
        assert plan[0]["args"] == {"action": "list_notebooks"}
        assert plan[0]["safe_call"]["function"]["arguments"] == raw

    def test_empty_arguments_dispatch_with_empty_dict(self):
        plan = _plan_voice_tool_calls([_tc("ping", "")])
        assert plan[0]["truncated"] is False
        assert plan[0]["args"] == {}

    def test_production_truncated_payload_not_dispatchable(self):
        raw = FIXTURE.read_text(encoding="utf-8")
        assert not raw.rstrip().endswith("}")
        plan = _plan_voice_tool_calls([_tc("notes", raw)])
        assert plan[0]["truncated"] is True
        assert plan[0]["args"] is None
        # provider-safe copy must be parseable; the original stays untouched
        # (forensic record — the truncated args are persisted verbatim).
        assert json.loads(plan[0]["safe_call"]["function"]["arguments"]) == {}
        assert plan[0]["call"]["function"]["arguments"] == raw

    def test_closed_but_invalid_json_not_dispatchable(self):
        plan = _plan_voice_tool_calls([_tc("notes", '{"action": }')])
        assert plan[0]["truncated"] is True
        assert plan[0]["args"] is None

    def test_mixed_calls_keep_order_and_classify_independently(self):
        raw = FIXTURE.read_text(encoding="utf-8")
        plan = _plan_voice_tool_calls([
            _tc("notes", '{"action": "list_notebooks"}'),
            _tc("notes", raw),
        ])
        assert [e["truncated"] for e in plan] == [False, True]
        assert plan[0]["args"]["action"] == "list_notebooks"


class TestTruncationFeedback:
    def test_feedback_is_valid_json_with_actionable_guidance(self):
        data = json.loads(_voice_truncation_feedback("notes"))
        assert data.get("_truncated") is True
        err = data["error"]
        assert "截断" in err and "分块" in err
        assert "notes" in err


class TestBudgetEscalation:
    def test_doubles_until_ceiling(self):
        assert _escalated_voice_max_tokens(1024, 8192) == 2048
        assert _escalated_voice_max_tokens(4096, 8192) == 8192

    def test_never_exceeds_ceiling(self):
        assert _escalated_voice_max_tokens(4096, 6144) == 6144
        assert _escalated_voice_max_tokens(8192, 8192) == 8192

    def test_unset_or_zero_budget_goes_to_ceiling(self):
        assert _escalated_voice_max_tokens(None, 8192) == 8192
        assert _escalated_voice_max_tokens(0, 8192) == 8192

    def test_missing_ceiling_keeps_current(self):
        assert _escalated_voice_max_tokens(1024, None) == 1024
