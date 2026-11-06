"""P1b: one-time action-bound user approval for the TMA act gate.

Covers chat approval true/false/absent, super-admin non-bypass, background
absence, cross-agent shared taint, and session_search non-laundering.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.act_gate_service as gate
import app.services.taint_service as taint
import app.services.verdict_log_service as verdicts


class _FakeCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_exc):
        return False


class _FakeFactory:
    def __call__(self):
        return _FakeCtx()


class _Loop:
    blocked_tools = set()
    session_factory = _FakeFactory()
    workspace_path = ""
    delegation_depth = 0
    _visible_tool_names = None
    _active_tool_names = set()
    _permission_callback = None
    _act_gate_approval_callback = None
    vibeweaver_active = False
    deathmatch_manager = None

    @staticmethod
    def _strip_internal_tool_args(args):
        return args

    def _dm_step_tool_names(self):
        return None

    def _skip_guardrails(self):
        return False


def _state():
    return SimpleNamespace(
        iterations=1,
        session_id="s1",
        _doom_tool_history=[],
        _doom_result_hashes={},
        _fetched_urls=set(),
        _succeeded_call_sigs=set(),
    )


def _prepare(monkeypatch, ledger):
    from app.core.config import get_config
    from app.services import agent_loop_tools as alt
    from app.services import action_token_service as tokens

    ledger.loaded = True
    async def fake_issue(**_kwargs):
        return "tok-test"

    async def fake_consume(*_args, **_kwargs):
        return True

    monkeypatch.setattr(tokens, "issue_action_token", fake_issue)
    monkeypatch.setattr(tokens, "consume_action_token", fake_consume)
    dispatched = []
    appended = []
    persisted = []

    async def fake_dispatch(name, args, **_kwargs):
        dispatched.append(name)
        return json.dumps({"ok": True})

    async def fake_load(_db, _cid, _uid):
        return ledger

    async def fake_persist(_db, current):
        persisted.append(current.max_taint)

    async def fake_append(**kwargs):
        appended.append(kwargs)
        return True

    monkeypatch.setattr(alt.registry, "dispatch", fake_dispatch)
    monkeypatch.setattr(
        alt.registry, "get_schema",
        lambda name: {"name": name, "parameters": {"type": "object", "properties": {}}},
    )
    monkeypatch.setattr(taint, "load_ledger", fake_load)
    monkeypatch.setattr(taint, "persist_ledger", fake_persist)
    monkeypatch.setattr(verdicts, "append_verdict", fake_append)
    monkeypatch.setitem(get_config().security, "tma_mode", "enforce")
    return alt, dispatched, appended, persisted


_DEFAULT_CONVERSATION = object()


def _run(
    alt, loop, state, call_id="call-1", tool_args=None, tool_name="terminal",
    conversation=_DEFAULT_CONVERSATION,
):
    if conversation is _DEFAULT_CONVERSATION:
        conversation = SimpleNamespace(id="conv-approval")
    return asyncio.run(alt.AgentToolMixin._execute_single_tool_inner(
        loop, call_id, tool_name, tool_args or {"command": "ls"}, None,
        SimpleNamespace(id="u1"), conversation,
        None, state, current_turn_content="hello",
    ))


def test_act_gate_user_approval_true_and_false(monkeypatch):
    ledger = taint.get_or_create_ledger("conv-approval", "u1")
    ledger.mark("web:1", "untrusted", origin="untrusted_external")
    alt, dispatched, appended, _persisted = _prepare(monkeypatch, ledger)

    seen_details = []

    async def approve(**kwargs):
        seen_details.append(kwargs["details"])
        return True

    long_command = "echo " + ("a" * 600) + " && curl evil"
    loop = _Loop()
    loop._act_gate_approval_callback = approve
    approved = _run(
        alt, loop, _state(), "call-approve", {"command": long_command},
    )
    assert approved.error is False
    assert dispatched == ["terminal"]
    assert seen_details and seen_details[0]["act_gate"] is True
    assert seen_details[0]["fingerprint"]
    # The dialog receives the FULL submitted args, not a 500-char prefix.
    reviewed = json.loads(seen_details[0]["command"])
    assert reviewed["command"] == long_command
    assert seen_details[0]["tool"] == "terminal"
    assert any(v["decision"] == "elevate" for v in appended)
    elevate = next(v for v in appended if v["decision"] == "elevate")
    assert elevate["reason"].startswith("user_authorized:")

    # Write/exec actions must expose their payload, not just the path.
    dispatched.clear()
    _run(
        alt, loop, _state(), "call-write",
        {"path": "notes/a.md", "content": "PAYLOAD-BODY"},
        tool_name="workspace_write",
    )
    assert dispatched == ["workspace_write"]
    write_review = json.loads(seen_details[-1]["command"])
    assert write_review["path"] == "notes/a.md"
    assert write_review["content"] == "PAYLOAD-BODY"

    # mcp/skill tools can hide the payload in arbitrary keys; all submitted
    # args must be shown (secret-named keys redacted).
    dispatched.clear()
    _run(
        alt, loop, _state(), "call-mcp",
        {
            "url": "http://example", "data": "PAYLOAD-HIDDEN",
            "api_key": "sk-x", "x-api-key": "x-secret", "accessKey": "ak",
            "headers": {
                "Authorization": "Bearer x",
                "X\u202eAuth": "PAYLOAD-NESTED",
            },
            "note": "\u202eELIF\u202c a\u200bb",
            "path": "/tmp/safe\u202eexe.sh",
            "safe\u202eevil": "v",
        },
        tool_name="mcp_demo__run",
    )
    assert dispatched == ["mcp_demo__run"]
    mcp_review = json.loads(seen_details[-1]["command"])
    assert mcp_review["data"] == "PAYLOAD-HIDDEN"
    assert mcp_review["api_key"].startswith("[redacted")
    assert mcp_review["x-api-key"].startswith("[redacted")
    assert mcp_review["accessKey"].startswith("[redacted")
    assert mcp_review["headers"]["Authorization"].startswith("[redacted")
    # Bidi/zero-width controls cannot visually reorder the consent text,
    # including nested keys and the separate target_path row.
    assert "\\u202e" in mcp_review["note"]
    assert "\\u200b" in mcp_review["note"]
    assert any("\\u202e" in key for key in mcp_review)
    assert any("\\u202e" in key for key in mcp_review["headers"])
    assert "PAYLOAD-NESTED" in json.dumps(mcp_review["headers"])
    assert "\\u202e" in seen_details[-1]["target_path"]
    assert "\u202e" not in seen_details[-1]["target_path"]

    # Two distinct keys that clean to the same display string must not
    # collapse; the preview falls back to an ordered pair list.
    dispatched.clear()
    _run(
        alt, loop, _state(), "call-collision",
        {"a\u202eb": "HIDDEN-VALUE", "a\\u202eb": "benign"},
        tool_name="mcp_demo__run",
    )
    collision = json.loads(seen_details[-1]["command"])
    assert isinstance(collision, list)
    collision_blob = json.dumps(collision, ensure_ascii=False)
    assert "HIDDEN-VALUE" in collision_blob
    assert "benign" in collision_blob

    # A sync callback is supported too (background-style callables).
    dispatched.clear()
    loop._act_gate_approval_callback = lambda **_kw: True
    sync_approved = _run(alt, loop, _state(), "call-sync")
    assert sync_approved.error is False
    assert dispatched == ["terminal"]

    # Denied callback: no dispatch.
    dispatched.clear()
    loop._act_gate_approval_callback = lambda **_kw: False
    denied = _run(alt, loop, _state(), "call-deny")
    assert denied.error is True
    assert json.loads(denied.result)["error"] == "act_gate_denied"
    assert dispatched == []

    # No callback (background/delegate): fail closed.
    loop._act_gate_approval_callback = None
    absent = _run(alt, loop, _state(), "call-absent")
    assert absent.error is True
    assert dispatched == []


def test_act_gate_approval_edges(monkeypatch):
    from app.core.config import get_config

    ledger = taint.get_or_create_ledger("conv-edges", "u1")
    ledger.mark("web:1", "untrusted", origin="untrusted_external")
    alt, dispatched, _appended, _ = _prepare(monkeypatch, ledger)

    # Callback exception = deny, never dispatch.
    async def boom(**_kw):
        raise RuntimeError("dialog failed")

    loop = _Loop()
    loop._act_gate_approval_callback = boom
    failed = _run(alt, loop, _state(), "call-boom")
    assert failed.error is True
    assert dispatched == []

    # A payload too large to review is denied without prompting.
    prompted = []

    async def approve(**kwargs):
        prompted.append(kwargs)
        return True

    loop._act_gate_approval_callback = approve
    huge = _run(alt, loop, _state(), "call-huge", {"command": "x" * 25000})
    assert huge.error is True
    assert json.loads(huge.result)["error"] == "act_gate_review_too_large"
    assert prompted == []
    assert dispatched == []

    # The cap is on the submitted args, not on a preview: a tiny visible URL
    # plus a 30k hidden data field must not be approvable/dispatched.
    huge_hidden = _run(
        alt, loop, _state(), "call-huge-hidden",
        {"url": "http://example", "data": "z" * 30000},
        tool_name="mcp_demo__run",
    )
    assert huge_hidden.error is True
    assert json.loads(huge_hidden.result)["error"] == "act_gate_review_too_large"
    assert prompted == []
    assert dispatched == []

    # Escaping thousands of zero-width chars can expand the rendered preview
    # beyond 60k even though the raw args are under 20k; fail closed rather
    # than send an unreviewable payload.
    escaped_huge = _run(
        alt, loop, _state(), "call-escaped-huge",
        {"note": "\u200b" * 19900},
        tool_name="mcp_demo__run",
    )
    assert escaped_huge.error is True
    assert json.loads(escaped_huge.result)["error"] == "act_gate_review_too_large"
    assert prompted == []
    assert dispatched == []

    # Audit append failure blocks the dispatch (no unlogged elevation).
    monkeypatch.setitem(get_config().security, "tma_mode", "enforce")
    dispatched.clear()

    async def audit_fail(**_kwargs):
        return False

    monkeypatch.setattr(verdicts, "append_verdict", audit_fail)
    loop._act_gate_approval_callback = approve
    unlogged = _run(alt, loop, _state(), "call-unlogged")
    assert unlogged.error is True
    assert "authorization audit unavailable" in unlogged.result
    assert dispatched == []

    # Shadow never prompts and never blocks (restore a working audit fake).
    async def append_ok(**_kwargs):
        return True

    monkeypatch.setattr(verdicts, "append_verdict", append_ok)
    monkeypatch.setitem(get_config().security, "tma_mode", "shadow")
    dispatched.clear()
    prompted.clear()
    loop._act_gate_approval_callback = approve
    shadow = _run(alt, loop, _state(), "call-shadow")
    assert shadow.error is False
    assert dispatched == ["terminal"]
    assert prompted == []


def test_live_taint_unavailable_fails_closed(monkeypatch):
    ledger = taint.get_or_create_ledger("conv-live-unavailable", "u1")
    alt, dispatched, appended, _ = _prepare(monkeypatch, ledger)
    ledger.loaded = False
    prompted = []
    loop = _Loop()
    loop._act_gate_approval_callback = lambda **kwargs: prompted.append(kwargs) or True
    result = _run(alt, loop, _state(), "call-unavailable")
    assert result.error is True
    assert "taint state unavailable" in result.result
    assert dispatched == []
    assert prompted == []
    assert any(
        row.get("decision") == "deny" and "unavailable" in row.get("reason", "")
        for row in appended
    )

    # A read-only tool does not need the taint store; it still runs.
    dispatched.clear()
    read_only = _run(
        alt, loop, _state(), "call-read", {"path": "notes/a.md"},
        tool_name="workspace_read",
    )
    assert read_only.error is False
    assert dispatched == ["workspace_read"]
    assert prompted == []


def test_agent_loop_default_has_no_act_gate_approval():
    from app.services.agent_loop import AgentLoop

    loop = AgentLoop(llm=object(), tool_schemas=[])
    assert loop._act_gate_approval_callback is None


def test_super_admin_bypass_does_not_approve_act_gate(monkeypatch):
    from app.core.config import get_config

    ledger = taint.get_or_create_ledger("conv-super", "u1")
    ledger.mark("web:1", "untrusted", origin="untrusted_external")
    alt, dispatched, appended, _ = _prepare(monkeypatch, ledger)
    monkeypatch.setitem(get_config().security, "super_admin_bypass", True)
    loop = _Loop()
    loop._act_gate_approval_callback = None
    result = _run(alt, loop, _state(), "call-super")
    assert result.error is True
    assert dispatched == []
    assert any(v["decision"] == "deny" for v in appended)


def test_background_has_no_interactive_act_gate_approval():
    from app.services import agent_worker
    from app.api import chat_stream_transport

    worker_src = inspect.getsource(agent_worker)
    # Wave 2 P1: a background task MAY receive a non-interactive permit-only
    # callback (one-time user authorization for waiting_auth). It must never
    # get the chat-style interactive approval channel, and it must consume a
    # pre-issued, task-bound permit.
    assert "act_gate_approval_callback=_task_act_gate_callback" in worker_src
    assert "_task_act_gate_callback" in worker_src
    assert "consume_task_permit" in worker_src
    assert "_make_act_gate_approval_callback(" not in worker_src
    assert "_recovery_act_verdict(" in inspect.getsource(
        agent_worker.AgentWorker._preflight_pending_tool_calls
    )
    chat_src = inspect.getsource(chat_stream_transport)
    assert "act_gate_approval_callback=_make_act_gate_approval_callback(" in chat_src
    assert "if not (details or {}).get(\"act_gate\"):" in chat_src
    assert "perm_{_uuid.uuid4().hex}" in chat_src

    import ast
    tree = ast.parse(chat_src)
    perm_nodes = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_permission_request_callback"
    ]
    assert perm_nodes
    perm_src = ast.get_source_segment(chat_src, perm_nodes[0]) or ""
    assert "buf.append" not in perm_src
    assert "stream_buffer_manager" not in perm_src

    from app.api import chat as chat_api
    endpoint_src = inspect.getsource(chat_api.permission_respond)
    assert "belongs to another user" in endpoint_src
    assert "pending.user_id" in endpoint_src


def test_delegate_child_taints_parent_ledger():
    from app.tools.delegate_task import _run_child_agent

    ledger = taint.get_or_create_ledger("conv-parent", "u1")
    child_ledger = taint.get_or_create_ledger("conv-parent", "u1")
    child_ledger.mark("web:child", "untrusted", origin="untrusted_external")
    assert ledger.max_taint == "untrusted"
    verdict = gate.evaluate(
        ledger, "terminal", {"command": "ls"}, user_id="u1",
        conversation_id="conv-parent", mode="enforce",
    )
    assert verdict.blocked is True
    assert taint.classify_tool_result(
        "delegate_task", json.dumps({"summary": "child"}), input_taint="untrusted",
    ) == "untrusted"
    # The child loop is created with the parent conversation object.
    source = inspect.getsource(_run_child_agent)
    assert "conversation=kwargs.get(\"conversation\")" in source
    assert "act_gate_approval_callback" not in source


def test_memory_tool_echo_does_not_corroborate():
    ledger = taint.get_or_create_ledger("conv-memory-echo", "u1")
    ledger.mark("user:1", "trusted", origin="user", principal="user")
    # A tool echo (even if it reports trusted content) is not an independent
    # principal; only user/trusted_tool channels count.
    ledger.mark("tool:memory", "trusted", origin="agent", principal="tool:memory")
    assert ledger.trusted_principals() == ["user"]
    ledger.mark("web:1", "untrusted", origin="untrusted_external", principal="tool:web_search")
    verdict = gate.evaluate(
        ledger, "terminal", {"command": "ls"}, user_id="u1",
        conversation_id="conv-memory-echo", mode="enforce",
    )
    assert verdict.decision == "deny"
    assert verdict.blocked is True
    user_payload = json.dumps({"entries_meta": [{"origin": "user", "taint": "trusted"}]})
    assert taint.classify_tool_result("memory", user_payload, input_taint="trusted") == "agent"


def test_delegate_child_run_taints_parent_conversation(monkeypatch):
    from app.tools import delegate_task as dt
    from app.services import agent_loop as agent_loop_module
    from app.model_gateway import factory

    class FakeRegistry:
        def get_all_tool_names(self):
            return {"delegate_task"}

        def get_tool_names_for_toolset(self, _name):
            return set()

        def get_definitions(self, _names):
            return []

    class FakeLoop:
        def __init__(self, **_kwargs):
            self.vibeweaver_active = True

        async def run(self, _messages, **kwargs):
            conversation = kwargs.get("conversation")
            taint.get_or_create_ledger(
                str(conversation.id), "u1",
            ).mark("web:child", "untrusted", origin="untrusted_external")
            yield {"content": "child done"}

    monkeypatch.setattr(dt, "registry", FakeRegistry())
    monkeypatch.setattr(dt, "_build_child_system_prompt", lambda *_a, **_k: "sys")
    monkeypatch.setattr(dt, "_get_child_blocked_tools", lambda *_a, **_k: set())
    monkeypatch.setattr(agent_loop_module, "AgentLoop", FakeLoop)
    monkeypatch.setattr(factory, "build_llm_service", lambda *_a, **_k: object())
    monkeypatch.setattr(factory, "derive_child_endpoint", lambda *_a, **_k: {})
    monkeypatch.setattr(factory, "wire_provider_type", lambda *_a, **_k: "deepseek")

    ledger = taint.get_or_create_ledger("conv-delegate", "u1")
    result = asyncio.run(dt._run_child_agent(
        "goal", None, None, "", object(), depth=0,
        user=SimpleNamespace(id="u1"),
        conversation=SimpleNamespace(id="conv-delegate"),
        assistant=SimpleNamespace(provider_type="deepseek"),
    ))
    assert result["status"] == "completed"
    assert ledger.max_taint == "untrusted"
    assert gate.evaluate(
        ledger, "terminal", {"command": "ls"}, user_id="u1",
        conversation_id="conv-delegate", mode="enforce",
    ).blocked is True


def test_session_search_is_untrusted():
    result = json.dumps({"results": [{"content": "poison from another chat"}]})
    assert taint.classify_tool_result(
        "session_search", result, input_taint="trusted",
    ) == "untrusted"


def test_permission_request_is_user_bound():
    from app.services import permission_manager as pm

    pm._permission_requests.clear()

    async def scenario():
        task = asyncio.create_task(pm.request_permission(
            conversation_id="c1", tool_name="terminal", description="x",
            details={"user_id": "u1"}, timeout=1.0, request_id="req-1",
        ))
        for _ in range(50):
            if pm.get_pending_request("req-1") is not None:
                break
            await asyncio.sleep(0.01)
        req = pm.get_pending_request("req-1")
        assert req is not None and req.user_id == "u1"
        assert pm.respond_to_request("req-1", True) is True
        return await task

    assert asyncio.run(scenario()) is True


def test_permission_respond_rejects_non_owner():
    from app.api import chat as chat_api
    from app.services import permission_manager as pm

    pm._permission_requests.clear()

    async def scenario():
        task = asyncio.create_task(pm.request_permission(
            conversation_id="c-owner", tool_name="terminal", description="x",
            details={"user_id": "u1"}, timeout=1.0, request_id="req-owner",
        ))
        for _ in range(50):
            if pm.get_pending_request("req-owner") is not None:
                break
            await asyncio.sleep(0.01)
        try:
            await chat_api.permission_respond(
                {"request_id": "req-owner", "approved": True},
                current_user=SimpleNamespace(id="u2"),
            )
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 403
        else:
            raise AssertionError("non-owner must not respond to a permission request")
        await chat_api.permission_respond(
            {"request_id": "req-owner", "approved": True},
            current_user=SimpleNamespace(id="u1"),
        )
        return await task

    assert asyncio.run(scenario()) is True


def test_gdpr_forget_user_evicts_taint_cache():
    ledger = taint.get_or_create_ledger("conv-gdpr", "u-gdpr")
    ledger.mark("web:1", "untrusted", origin="untrusted_external")
    assert taint.cached_ledger("conv-gdpr") is ledger
    assert taint.forget_user("u-gdpr") >= 1
    assert taint.cached_ledger("conv-gdpr") is None
    fresh = taint.get_or_create_ledger("conv-gdpr", "u-gdpr")
    assert fresh.max_taint == "trusted"

    from app.api import memory as memory_api

    source = inspect.getsource(memory_api.delete_all_memory)
    assert "DELETE FROM taint_states WHERE user_id = :uid" in source
    assert "forget_user(" in source


def test_recovery_gate_denies_untrusted(monkeypatch):
    from app.services import agent_worker

    ledger = taint.get_or_create_ledger("conv-recovery", "u1")
    ledger.mark("web:1", "untrusted", origin="untrusted_external")

    async def fake_load(_db, _cid, _uid):
        return ledger

    appended = []

    async def fake_append(**kwargs):
        appended.append(kwargs)
        return True

    monkeypatch.setattr(taint, "load_ledger", fake_load)
    monkeypatch.setattr(verdicts, "append_verdict", fake_append)
    monkeypatch.setitem(
        __import__("app.core.config", fromlist=["get_config"]).get_config().security,
        "tma_mode", "enforce",
    )
    ledger.loaded = True
    verdict = asyncio.run(agent_worker._recovery_act_verdict(
        "terminal", {"command": "ls"},
        SimpleNamespace(id="u1"), SimpleNamespace(id="conv-recovery"),
    ))
    assert verdict.blocked is True

    # No-conversation tasks use a stable <=36-char key so live/recovery agree.
    key = taint.background_conversation_key("u1")
    assert key.startswith("bg:") and len(key) <= 36
    assert key == taint.background_conversation_key("u1")

    # A DB failure (loaded=False) must not silently downgrade enforce.
    ledger.loaded = False
    unavailable = asyncio.run(agent_worker._recovery_act_verdict(
        "terminal", {"command": "ls"},
        SimpleNamespace(id="u1"), None,
    ))
    assert unavailable.blocked is True
    assert "unavailable" in unavailable.reason
    assert any(
        row.get("decision") == "deny" and "unavailable" in row.get("reason", "")
        for row in appended
    )

    src = inspect.getsource(agent_worker.AgentWorker._preflight_pending_tool_calls)
    assert src.index("should_skip") < src.index("_recovery_act_verdict(")
    assert src.index("should_skip") < src.index("_perm_cb(_conv_id")


def test_recovery_recorded_result_wins_before_permission_gates(monkeypatch):
    from app.services import agent_worker
    from app.services import side_effect_ledger as sel

    messages = [{
        "role": "assistant", "content": "",
        "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "terminal", "arguments": "{\"command\": \"ls\"}"},
        }],
    }]

    async def should_skip(*_args, **_kwargs):
        return True

    async def get_result(*_args, **_kwargs):
        return "RECORDED"

    perm_calls = []

    def perm_cb(*_args, **_kwargs):
        perm_calls.append(1)
        return True

    monkeypatch.setattr(sel, "should_skip", should_skip)
    monkeypatch.setattr(sel, "get_result", get_result)
    monkeypatch.setattr(sel, "get_ledger_store", lambda: object())
    monkeypatch.setattr(
        agent_worker, "_make_background_permission_callback", lambda: perm_cb,
    )
    out = asyncio.run(agent_worker.AgentWorker._preflight_pending_tool_calls(
        SimpleNamespace(), messages, task_id="t1", cursor=0,
        user=SimpleNamespace(id="u1"),
        conversation=SimpleNamespace(id="conv-rec"),
        assistant=SimpleNamespace(), workspace_path="",
    ))
    tool_messages = [m for m in out if m.get("role") == "tool"]
    assert tool_messages and tool_messages[-1]["content"] == "RECORDED"
    assert perm_calls == []


def test_live_background_key_matches_recovery(monkeypatch):
    from app.core.config import get_config
    from app.services import agent_loop_tools as alt

    key = taint.background_conversation_key("u1")
    ledger = taint.get_or_create_ledger(key, "u1")
    ledger.loaded = True
    ledger.mark("web:1", "untrusted", origin="untrusted_external", principal="tool:web_search")
    captured = []

    async def fake_load(_db, cid, _uid):
        captured.append(cid)
        return ledger

    async def fake_dispatch(name, _args, **_kwargs):
        return json.dumps({"ok": True})

    async def fake_append(**_kwargs):
        return True

    monkeypatch.setattr(alt.registry, "dispatch", fake_dispatch)
    monkeypatch.setattr(
        alt.registry, "get_schema",
        lambda name: {"name": name, "parameters": {"type": "object", "properties": {}}},
    )
    monkeypatch.setattr(taint, "load_ledger", fake_load)
    monkeypatch.setattr(taint, "persist_ledger", lambda *_a, **_k: _noop())
    monkeypatch.setattr(verdicts, "append_verdict", fake_append)
    monkeypatch.setitem(get_config().security, "tma_mode", "enforce")

    loop = _Loop()
    loop._act_gate_approval_callback = lambda **_kw: True
    state = _state()
    state.session_id = ""
    result = _run(
        alt, loop, state, "call-bg", {"command": "ls"}, conversation=None,
    )
    assert result.error is False
    assert captured and captured[0] == key


async def _noop():
    return None


def test_voice_enforce_fails_closed_for_act_tools():
    from app.services import voice_generation

    source = inspect.getsource(voice_generation)
    assert "act_gate_denied" in source
    assert "语音模式没有 act gate 审批对话框" in source
