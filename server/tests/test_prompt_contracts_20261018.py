"""P1.4 output-contract tests (2026-10-18).

Covers every failure mode the plan names: valid, missing field, extra field,
truncated, empty, non-JSON, Unicode/escapes, and one bounded repair retry.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.prompts.contracts import (
    AuditVerdict,
    ContractErrorKind,
    JudgeVerdict,
    ScheduleResult,
    TitleResult,
    contract_for,
    extract_json_value,
    parse_contract,
    parse_contract_with_repair,
)
from app.prompts.registry import all_specs


def test_every_declared_output_schema_has_a_contract():
    missing = [
        (spec.id, spec.output_schema)
        for spec in all_specs()
        if spec.output_schema and contract_for(spec.output_schema) is None
    ]
    assert missing == []


def test_valid_title_contract():
    value, failure = parse_contract(TitleResult, '{"title": "Python 简介"}')
    assert failure is None
    assert value.title == "Python 简介"


def test_missing_required_field_is_schema_failure():
    value, failure = parse_contract(TitleResult, "{}")
    assert value is None
    assert failure.kind == ContractErrorKind.SCHEMA


def test_extra_fields_are_ignored():
    value, failure = parse_contract(
        TitleResult, '{"title": "标题", "extra": {"nested": [1, 2]}}')
    assert failure is None
    assert value.title == "标题"


def test_truncated_json_is_classified():
    value, failure = extract_json_value('{"title": "abc')
    assert value is None
    assert failure.kind == ContractErrorKind.TRUNCATED


def test_empty_response_is_classified():
    _, failure = extract_json_value("   ")
    assert failure.kind == ContractErrorKind.EMPTY


def test_non_json_is_classified():
    _, failure = extract_json_value("not json at all")
    assert failure.kind == ContractErrorKind.NON_JSON


def test_unicode_and_escapes_roundtrip():
    raw = json.dumps({"verdict": "done", "reason": "完成：\\n\\t“引号” ✅ 中文"},
                     ensure_ascii=False)
    value, failure = parse_contract(JudgeVerdict, raw)
    assert failure is None
    assert "完成" in value.reason
    assert "中文" in value.reason


def test_repair_retry_is_bounded_and_reported():
    calls = []

    def repair(raw, failure):
        calls.append(failure.kind)
        return '{"title": "修复后标题"}'

    value, failure, repaired = parse_contract_with_repair(
        TitleResult, '{"title":', repair)
    assert failure is None
    assert repaired is True
    assert value.title == "修复后标题"
    assert calls == [ContractErrorKind.TRUNCATED]


def test_failed_repair_returns_failure_not_silent_default():
    value, failure, repaired = parse_contract_with_repair(
        TitleResult, "nope", lambda raw, failure: "still nope")
    assert value is None
    assert failure is not None
    assert repaired is False


def test_audit_contract_preserves_provider_extras():
    value, failure = parse_contract(
        AuditVerdict,
        '{"verdict": "reject", "problem": "缺引用", "unsupported_claims": [{"x": 1}]}',
    )
    assert failure is None
    assert value.verdict == "reject"
    assert value.problem == "缺引用"
    # extra fields are preserved for existing dict consumers
    assert value.model_dump()["unsupported_claims"] == [{"x": 1}]


def test_schedule_contract_accepts_nested_optional_shape():
    value, failure = parse_contract(
        ScheduleResult,
        '{"schedule_type": "weekly", "dow": 1, "hour": 20, "minute": 30}',
    )
    assert failure is None
    assert value.schedule_type == "weekly"
    assert value.dow == 1


def test_all_declared_schemas_validate_representative_payloads():
    from app.prompts.contracts import contract_for

    samples = {
        "audit.verdict": '{"verdict":"reject","problem":"x"}',
        "deathmatch.judge": '{"verdict":"done","reason":"ok"}',
        "deathmatch.verifier": '{"status":"complete","issues":[]}',
        "title.generation": '{"title":"标题"}',
        "schedule.parse": '{"schedule_type":"daily","hour":7}',
        "coordinator.route": '{"route":"research","focus":"x"}',
        "claim.verify": '{"verdict":"pass","reason":"ok"}',
        "audit.scout": '{"ids":[1],"keywords":["k"]}',
        "error.classify": '{"category":"timeout"}',
        "memory.extraction": '{"concepts":[{"name":"x"}]}',
        "search.plan": '{"queries":["q"]}',
        "deathmatch.grilling": '{"questions":[{"question":"q"}]}',
        "deathmatch.plan": '{"steps":[{"id":"s1"}]}',
    }
    for schema, raw in samples.items():
        model = contract_for(schema)
        assert model is not None, schema
        value, failure = parse_contract(model, raw)
        assert failure is None, (schema, failure)
        assert value is not None


def test_error_classifier_missing_category_falls_back_to_timeout(monkeypatch):
    """A missing category must keep the old TIMEOUT fallback, not become UNKNOWN."""
    import asyncio
    from app.services import agentic_judge
    from app.services.error_classifier import ErrorCategory, ErrorClassifier

    async def fake_judge(*args, **kwargs):
        return {}

    monkeypatch.setattr(agentic_judge, "judge_json", fake_judge)
    assert asyncio.run(ErrorClassifier.classify_message("provider boom")) == ErrorCategory.TIMEOUT


def test_bracketed_prose_does_not_break_object_extraction():
    raw = '分析 [1] 如下：\n{"verdict": "reject", "problem": "缺引用"}\n(见 [2])'
    value, failure = parse_contract(AuditVerdict, raw)
    assert failure is None
    assert value.verdict == "reject"
    assert value.problem == "缺引用"


def test_missing_audit_verdict_stays_empty_for_conservative_reject():
    from app.services.agent_loop import _parse_audit_json

    parsed = _parse_audit_json('{"ok": false, "problem": "缺少证据"}')
    assert parsed is not None
    assert parsed["verdict"] == ""
    # _verdict_of maps an unknown/empty verdict to reject (legacy contract).
    ok = parsed.get("ok")
    v = str(parsed.get("verdict") or "").strip().lower()
    assert not ok and v not in ("reject", "unverifiable", "needs_evidence")
