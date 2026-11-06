"""PLAN T5/T6 — 远端权限哨兵、用户批准与带外 grant 出境。"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.services.agent_loop import AgentLoop, AgentLoopState
from app.services.device_session_service import device_session_manager

TEST_SECRET = "test-only-pairing-secret"  # vw-approved: deterministic unit-test key only


class _RemoteResult(str):
    _remote_execution = True


class _FakeDBContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


def _make_loop(workspace):
    return AgentLoop(
        llm=SimpleNamespace(),
        coordinator_llm=SimpleNamespace(),
        audit_llm=SimpleNamespace(),
        tool_schemas=[],
        workspace_path=str(workspace),
        blocked_tools=set(),
        session_factory=lambda: _FakeDBContext(),
    )


def _permissions_result():
    return {
        "_permission_needed": True,
        "_permission_key": "terminal_execution",
        "_permission_description": "device supplied benign text",
        "_command": "echo safe",
    }


def _prepare(monkeypatch, *, remote=True):
    from app.core.config import get_config
    from app.services import agent_loop_tools as alt

    monkeypatch.setattr(type(get_config()), "super_admin_bypass", property(lambda self: False))
    monkeypatch.setitem(get_config().security, "tma_mode", "off")
    calls = []

    async def fake_dispatch(name, args, **kwargs):
        calls.append({"name": name, "args": dict(args), "kwargs": dict(kwargs)})
        if len(calls) == 1:
            payload = json.dumps(_permissions_result())
            return _RemoteResult(payload) if remote else payload
        return json.dumps({"ok": True, "platform": "Darwin"})

    monkeypatch.setattr(alt.registry, "dispatch", fake_dispatch)
    return alt, calls


def _user(allowed=True):
    return SimpleNamespace(
        id="u-device-owner",
        username="tester",
        agent_permissions={"terminal_execution": True} if allowed else {},
    )


def _conversation():
    return SimpleNamespace(id="conv-device", execution_target="device:abc")


def test_remote_auto_allowed_still_requires_callback_and_sends_valid_grant(tmp_path, monkeypatch):
    alt, calls = _prepare(monkeypatch, remote=True)
    loop = _make_loop(tmp_path)
    callback_calls = []

    async def approve(**kwargs):
        callback_calls.append(kwargs)
        return True

    loop._permission_callback = approve
    monkeypatch.setattr(
        device_session_manager, "pairing_secret_for", lambda _device_id: TEST_SECRET,
        raising=False,
    )

    result = asyncio.run(loop._execute_single_tool(
        "call-remote", "terminal", {"command": "uname -a"}, None,
        _user(allowed=True), _conversation(), None, AgentLoopState(),
    ))

    assert result.error is False
    assert len(callback_calls) == 1, "remote auto_allowed must not silently retry"
    assert len(calls) == 2
    assert callback_calls[0]["description"] != "device supplied benign text"
    assert "uname -a" in callback_calls[0]["details"]["command"]
    assert "echo safe" not in callback_calls[0]["details"]["command"]

    approved = calls[1]
    assert approved["args"]["_permission_granted"] is True
    grant = approved["kwargs"].get("_device_grant")
    assert grant is not None, "approved remote retry must carry a grant"
    assert "_device_grant" not in calls[0]["kwargs"]

    from app.services.device_grant_service import verify_device_grant
    from app.services.remote_dispatch_router import strip_internal_args

    assert verify_device_grant(
        TEST_SECRET,
        "abc",
        "terminal",
        strip_internal_args(approved["args"]),
        grant,
    ) is True


def test_remote_without_callback_returns_explicit_error_and_zero_retry(tmp_path, monkeypatch):
    alt, calls = _prepare(monkeypatch, remote=True)
    loop = _make_loop(tmp_path)
    loop._permission_callback = None

    result = asyncio.run(loop._execute_single_tool(
        "call-no-callback", "terminal", {"command": "uname -a"}, None,
        _user(allowed=False), _conversation(), None, AgentLoopState(),
    ))

    assert result.error is True
    assert "设备授权通道不可用" in result.result
    assert len(calls) == 1


def test_remote_missing_session_secret_fails_closed_without_retry(tmp_path, monkeypatch):
    alt, calls = _prepare(monkeypatch, remote=True)
    loop = _make_loop(tmp_path)

    async def approve(**_kwargs):
        return True

    loop._permission_callback = approve
    monkeypatch.setattr(
        device_session_manager, "pairing_secret_for", lambda _device_id: None,
        raising=False,
    )

    result = asyncio.run(loop._execute_single_tool(
        "call-no-secret", "terminal", {"command": "uname -a"}, None,
        _user(allowed=True), _conversation(), None, AgentLoopState(),
    ))

    assert result.error is True
    assert "设备授权通道不可用" in result.result
    assert len(calls) == 1


def test_remote_permission_key_ignores_device_spoofed_metadata(tmp_path, monkeypatch):
    alt, calls = _prepare(monkeypatch, remote=True)
    monkeypatch.setattr(
        sys.modules[__name__],
        "_permissions_result",
        lambda: {
            "_permission_needed": True,
            "_permission_key": "workspace_write",
            "_permission_description": "device supplied benign text",
            "_command": "echo safe",
        },
    )
    loop = _make_loop(tmp_path)
    callback_calls = []

    async def approve(**kwargs):
        callback_calls.append(kwargs)
        return True

    loop._permission_callback = approve
    monkeypatch.setattr(
        device_session_manager, "pairing_secret_for", lambda _device_id: TEST_SECRET,
        raising=False,
    )
    result = asyncio.run(loop._execute_single_tool(
        "call-spoofed-permission-key", "terminal", {"command": "uname -a"}, None,
        _user(allowed=True), _conversation(), None, AgentLoopState(),
    ))

    assert result.error is False
    assert callback_calls[0]["details"]["permission_key"] == "terminal_execution"
    from app.services.agent_worker import _make_background_permission_callback

    background_callback = _make_background_permission_callback()
    assert background_callback("conv-bg", "process", "process start", callback_calls[0]["details"]) is False


def test_local_permission_auto_allowed_behavior_is_unchanged(tmp_path, monkeypatch):
    alt, calls = _prepare(monkeypatch, remote=False)
    loop = _make_loop(tmp_path)
    callback_calls = []

    async def should_not_run(**kwargs):
        callback_calls.append(kwargs)
        return True

    loop._permission_callback = should_not_run
    result = asyncio.run(loop._execute_single_tool(
        "call-local", "terminal", {"command": "ls"}, None,
        _user(allowed=True),
        SimpleNamespace(id="conv-local-cloud", execution_target=None),
        None,
        AgentLoopState(),
    ))

    assert result.error is False
    assert callback_calls == []
    assert len(calls) == 2
    assert "_device_grant" not in calls[1]["kwargs"]


def test_background_permission_callback_honors_derived_permission_key():
    from app.services.agent_worker import _make_background_permission_callback

    callback = _make_background_permission_callback()
    assert callback(
        "conv-bg", "process", "process start",
        {"permission_key": "terminal_execution", "action": "process"},
    ) is False
    assert callback("conv-bg", "process", "process start", {"action": "start"}) is False
    assert callback("conv-bg", "process", "process read", {"action": "read"}) is True


def test_real_registry_approved_retry_carries_grant_on_first_remote_rpc(tmp_path, monkeypatch):
    from app.core.config import get_config
    from app.services import remote_dispatch_router as rdr

    monkeypatch.setattr(type(get_config()), "super_admin_bypass", property(lambda self: False))
    monkeypatch.setitem(get_config().security, "tma_mode", "off")
    rpc_calls = []

    async def fake_rpc(device_id, method, params, timeout=None, expected_user_id=None):
        rpc_calls.append(params)
        return {"ok": True, "content": "OUT"}

    async def no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(rdr, "rpc_call", fake_rpc, raising=False)
    monkeypatch.setattr(rdr, "record_exec_audit", no_audit)
    monkeypatch.setattr(device_session_manager, "device_user_id", lambda _device_id: "u-device-owner")
    monkeypatch.setattr(device_session_manager, "pairing_secret_for", lambda _device_id: TEST_SECRET, raising=False)

    loop = _make_loop(tmp_path)
    approvals = []

    async def approve(**kwargs):
        approvals.append(kwargs)
        return True

    loop._permission_callback = approve
    result = asyncio.run(loop._execute_single_tool(
        "call-real-registry", "terminal", {"command": "uname -a"}, None,
        _user(allowed=False), _conversation(), None, AgentLoopState(),
    ))

    assert result.error is False
    assert len(approvals) == 1
    assert len(rpc_calls) == 1, "first remote RPC after approval must already be granted"
    assert rpc_calls[0].get("grant") is not None


def test_remote_denied_callback_never_retries_or_signs(tmp_path, monkeypatch):
    alt, calls = _prepare(monkeypatch, remote=True)
    loop = _make_loop(tmp_path)

    async def deny(**_kwargs):
        return False

    loop._permission_callback = deny
    result = asyncio.run(loop._execute_single_tool(
        "call-denied", "terminal", {"command": "uname -a"}, None,
        _user(allowed=False), _conversation(), None, AgentLoopState(),
    ))

    assert result.error is True
    assert "用户拒绝" in result.result
    assert len(calls) == 1
    assert "_device_grant" not in calls[0]["kwargs"]
