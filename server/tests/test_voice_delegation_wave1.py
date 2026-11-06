"""RED tests (A4.8) — 语音委派 Wave 1 纯逻辑面。

Run: cd backend && python -m pytest tests/test_voice_delegation_wave1.py -v
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.model_gateway.schemas import ModelEndpoint  # noqa: E402

# Assembled at runtime so the source tree never contains a credential-shaped literal.
_FAKE_KEY = "sk" + "-unit-test-placeholder"


def _ep(provider_type, efforts, alias="x"):
    caps = {}
    if efforts is not None:
        caps = {"supports_reasoning": True, "reasoning_efforts": list(efforts)}
    return ModelEndpoint(
        alias=alias,
        provider_type=provider_type,
        model_name=f"{provider_type}-model",
        capabilities=caps,
        is_custom=True,
    )


def test_highest_reasoning_deepseek_is_max():
    from app.services.agent_run_spec import resolve_highest_reasoning

    r = resolve_highest_reasoning(_ep("deepseek", ["max", "high", "low"]))
    assert r.policy == "strict_highest"
    assert r.supported is True
    assert r.enabled is True
    assert r.effort == "max"
    assert r.scope == "main_agent"


def test_highest_reasoning_qwen_is_xhigh():
    from app.services.agent_run_spec import resolve_highest_reasoning

    assert resolve_highest_reasoning(_ep("qwen3.8_vllm", ["xhigh", "medium", "low"])).effort == "xhigh"


def test_highest_reasoning_doubao_is_high():
    from app.services.agent_run_spec import resolve_highest_reasoning

    assert resolve_highest_reasoning(_ep("doubao", ["high", "medium", "low"])).effort == "high"


def test_reasoning_toggle_only_is_enabled_without_effort():
    from app.services.agent_run_spec import resolve_highest_reasoning

    r = resolve_highest_reasoning(_ep("custom", []))
    assert r.supported is True
    assert r.enabled is True
    assert r.effort is None


def test_reasoning_profile_fallback_picks_deepest_not_tuple_first():
    from app.services.agent_run_spec import resolve_highest_reasoning

    # DeepSeekProfile.efforts is ascending ("low","high","max") — a
    # capability-less endpoint must still resolve to max.
    ep = ModelEndpoint(alias="legacy:deepseek", provider_type="deepseek", capabilities={})
    r = resolve_highest_reasoning(ep)
    assert r.supported is True
    assert r.effort == "max"


def test_reasoning_unsupported_is_rejected_by_strict_policy():
    from app.services.agent_run_spec import resolve_highest_reasoning

    r = resolve_highest_reasoning(ModelEndpoint(alias="plain", capabilities={}))
    assert r.supported is False
    assert r.enabled is False
    assert r.effort is None


def test_endpoint_fingerprint_is_nonsecret_and_stable():
    from app.services.agent_run_spec import endpoint_fingerprint

    ep = ModelEndpoint(
        alias="deepseek",
        provider_type="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key=_FAKE_KEY,
        model_name="deepseek-v4",
        params={"temperature": 0.7},
        is_custom=True,
    )
    fp1 = endpoint_fingerprint(ep)
    fp2 = endpoint_fingerprint(ep)
    assert fp1 == fp2
    assert "sk-secret-value" not in fp1
    assert "api.deepseek.com" not in fp1


def test_request_fingerprint_excludes_server_derived_paths():
    from app.services.agent_run_spec import canonical_request_fingerprint

    a = canonical_request_fingerprint(
        goal="调研 X", target_assistant_id="asst-1",
        reasoning_policy="strict_highest", reasoning_effort="max",
        source_conversation_id="c1", source_message_id="m1",
    )
    b = canonical_request_fingerprint(
        goal="  调研 X  ", target_assistant_id="asst-1",
        reasoning_policy="strict_highest", reasoning_effort="max",
        source_conversation_id="c1", source_message_id="m1",
    )
    c = canonical_request_fingerprint(
        goal="调研 Y", target_assistant_id="asst-1",
        reasoning_policy="strict_highest", reasoning_effort="max",
        source_conversation_id="c1", source_message_id="m1",
    )
    assert a == b
    assert a != c
    assert "agent_tasks/" not in a


def test_manifest_has_no_credentials_or_urls():
    from app.services.agent_run_spec import build_execution_manifest, resolve_highest_reasoning

    class _A:
        id = "asst-1"
        name = "研究助手"
        system_prompt = "你负责研究"
        model_alias = "deepseek"
        subtask_model_alias = None
        temperature = 0.3
        top_p = 0.9
        citation_style = "numeric"

    ep = ModelEndpoint(
        alias="deepseek",
        provider_type="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key=_FAKE_KEY,
        model_name="deepseek-v4",
        capabilities={"supports_reasoning": True, "reasoning_efforts": ["max"]},
        is_custom=True,
    )
    manifest = build_execution_manifest(
        assistant=_A(),
        endpoint=ep,
        reasoning=resolve_highest_reasoning(ep),
        source_conversation_id="c1",
        source_message_id="m1",
        context_cutoff="2026-11-04T00:00:00",
        workspace_task_root="agent_tasks/T1",
        iterations_max=40,
        wall_time_seconds=0,
    )
    dumped = json.dumps(manifest, ensure_ascii=False)
    assert manifest["version"] == 1
    assert manifest["reasoning"]["effort"] == "max"
    assert "sk-secret-value" not in dumped
    assert "api.deepseek.com" not in dumped
    assert manifest["workspace"]["scratch"] == "agent_tasks/T1/scratch"
    assert manifest["workspace"]["artifacts"] == "agent_tasks/T1/artifacts"


def test_agent_task_separate_columns_exist():
    from app.db.database import AgentTask

    for name in (
        "source_conversation_id", "source_message_id", "execution_conversation_id",
        "goal_message_id", "execution_mode", "origin_kind", "execution_config",
        "idempotency_key", "request_fingerprint", "attempt_id", "lease_until",
        "heartbeat_at", "cancel_requested_at", "status_version", "output_message_id",
        "reasoning_status", "runtime_meta", "usage_meta",
    ):
        assert getattr(AgentTask, name, None) is not None, name


def test_voice_outbox_v2_columns_exist():
    from app.db.database import VoiceNotificationOutbox

    for name in (
        "logical_task_id", "recipient_user_id", "origin_conversation_id",
        "execution_conversation_id", "output_message_id", "event_type",
        "status_version", "delivery_attempt_id", "delivery_lease_owner",
        "delivery_lease_expires_at", "dismissed_at",
    ):
        assert getattr(VoiceNotificationOutbox, name, None) is not None, name


def test_assistant_name_normalisation_and_uniqueness():
    from app.services.agent_task_submission_service import (
        AssistantResolutionError,
        normalize_assistant_name,
        resolve_unique_assistant,
    )

    assert normalize_assistant_name("  研究  助手 ") == "研究 助手"
    assert normalize_assistant_name("Study_Assistant") == "Study Assistant"

    class _A:
        def __init__(self, aid, name):
            self.id = aid
            self.name = name

    one = [_A("1", "研究助手")]
    assert resolve_unique_assistant(one, "研究助手").id == "1"
    with pytest.raises(AssistantResolutionError) as exc:
        resolve_unique_assistant(one, "不存在的助手")
    assert exc.value.code == "assistant_not_found"
    with pytest.raises(AssistantResolutionError) as exc:
        resolve_unique_assistant([_A("1", "研究助手"), _A("2", "研究 助手")], "研究助手")
    assert exc.value.code == "assistant_ambiguous"


def test_status_helpers_and_cancel_transition():
    from app.services.agent_task_finalize import (
        TERMINAL_STATUSES,
        cancel_decision,
        can_steer,
        is_terminal,
        is_separate,
    )

    class _T:
        def __init__(self, status, mode="separate_v1"):
            self.status = status
            self.execution_mode = mode

    assert is_separate(_T("pending"))
    assert not is_separate(_T("pending", mode=None))
    assert is_terminal(_T("completed")) and "completed" in TERMINAL_STATUSES
    assert is_terminal(_T("failed")) and is_terminal(_T("cancelled")) and is_terminal(_T("timed_out"))
    assert not is_terminal(_T("cancelling"))
    for status in ("pending", "claimed", "running", "resumable"):
        assert can_steer(_T(status)) is True
    for status in ("cancelling", "completed", "failed", "cancelled", "timed_out"):
        assert can_steer(_T(status)) is False
    assert cancel_decision(_T("pending")) == "cancelled_now"
    assert cancel_decision(_T("resumable")) == "cancelled_now"
    assert cancel_decision(_T("claimed")) == "cancelling"
    assert cancel_decision(_T("running")) == "cancelling"
    assert cancel_decision(_T("cancelling")) == "already_cancelling"
    assert cancel_decision(_T("completed")) == "too_late"


def test_separate_notification_payload_has_no_result_text():
    from app.services.voice_notification_service import build_separate_notification_payload

    class _T:
        id = "task-1"
        title = "价格调研"
        goal = "SECRET-GOAL-TEXT"
        result = "SECRET-RESULT-TEXT"
        error = "SECRET-ERROR-TEXT"
        status = "completed"
        user_id = "u1"
        source_conversation_id = "c1"
        execution_conversation_id = "c2"
        output_message_id = "m2"
        status_version = 3
        assistant_id = "a1"

    class _A:
        name = "研究助手"

    payload = build_separate_notification_payload(_T(), assistant_name=_A.name)
    dumped = json.dumps(payload, ensure_ascii=False)
    assert payload["logical_task_id"] == "task-1"
    assert payload["execution_conversation_id"] == "c2"
    assert payload["assistant_name"] == "研究助手"
    assert payload["notification_mode"] == "deterministic_v1"
    for secret in ("SECRET-GOAL-TEXT", "SECRET-RESULT-TEXT", "SECRET-ERROR-TEXT"):
        assert secret not in dumped


def test_migrations_define_separate_fields_before_pgvector_sentinel():
    from app.db.migrations import STARTUP_MIGRATIONS

    names = [name for name, _ in STARTUP_MIGRATIONS]
    required = {
        "agent_tasks_execution_mode",
        "agent_tasks_execution_conversation_id",
        "agent_tasks_idempotency_key",
        "agent_tasks_attempt_id",
        "agent_tasks_status_version",
        "uq_agent_tasks_user_idempotency",
        "uq_agent_tasks_execution_conversation",
        "voice_notification_outbox_logical_task_id",
        "voice_notification_outbox_recipient",
        "voice_notification_outbox_task_fk_set_null",
        "uq_voice_notification_outbox_event",
    }
    assert required <= set(names)
    sentinel = names.index("pgvector_extension")
    for name in required:
        assert names.index(name) < sentinel, name


def test_delegate_tool_fails_closed_when_flag_off(monkeypatch):
    import asyncio

    from app.core.config import get_config
    from app.tools.delegate_agent_task import delegate_agent_task

    cfg = get_config()
    monkeypatch.setitem(
        cfg._config.setdefault("voice_delegation", {}), "enabled", False)
    assert cfg.voice_delegation_enabled is False
    result = asyncio.run(delegate_agent_task(
        {"goal": "测试任务"},
        user=object(),
        conversation=object(),
        db=object(),
    ))
    payload = json.loads(result)
    assert payload["error"] == "voice_delegation_disabled"
    assert payload["recoverable"] is False


def test_terminal_message_text_contract():
    from app.services.agent_task_finalize import terminal_message_text

    text = terminal_message_text(
        status="failed", result=None, error="boom", assistant_name="研究助手")
    assert "失败" in text and "boom" in text
    timed = terminal_message_text(
        status="timed_out", result="部分结果", error=None, assistant_name="研究助手")
    assert "时限" in timed and "部分结果" in timed


def test_strict_reasoning_refuses_provider_fallback():
    import inspect

    from app.services.agent_loop import AgentLoop

    src = inspect.getsource(AgentLoop._try_fallback_provider)
    assert "strict_reasoning" in src
    assert "reasoning_degraded" in src


def test_reasoning_degraded_gate_excludes_dedicated_subtask_client():
    from app.services.agent_run_spec import should_mark_reasoning_degraded

    assert should_mark_reasoning_degraded(
        enabled=True, use_thinking=False, retry_thinking_off=True,
        revision_thinking_off=False, main_serves_iterations=True) is True
    assert should_mark_reasoning_degraded(
        enabled=True, use_thinking=False, retry_thinking_off=True,
        revision_thinking_off=False, main_serves_iterations=False) is False
    assert should_mark_reasoning_degraded(
        enabled=True, use_thinking=True, retry_thinking_off=True,
        revision_thinking_off=False, main_serves_iterations=True) is False
    assert should_mark_reasoning_degraded(
        enabled=False, use_thinking=False, retry_thinking_off=True,
        revision_thinking_off=True, main_serves_iterations=True) is False


def test_chat_origin_terminal_event_is_web_channel():
    from app.services.voice_notification_service import build_separate_notification_payload

    class _T:
        id = "task-chat"
        title = "chat task"
        status = "completed"
        user_id = "u1"
        source_conversation_id = "c1"
        execution_conversation_id = "c2"
        output_message_id = "m2"
        status_version = 1
        origin_kind = "chat"
        reasoning_status = "fulfilled"

    payload = build_separate_notification_payload(_T(), assistant_name="研究助手")
    assert payload["delivery_channel"] == "web"
