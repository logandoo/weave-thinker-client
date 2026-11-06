"""PLAN T6 — 单次设备授权 grant（HMAC-SHA256）与 WS 会话 secret。"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.services.device_session_service import DeviceSessionManager


SECRET = "test-only-pairing-secret"  # vw-approved: deterministic unit-test key only
DEVICE = "device-abc123"
ARGS = {"command": "uname -a", "nested": {"b": 2, "a": 1}}


def _functions():
    try:
        from app.services.device_grant_service import issue_device_grant, verify_device_grant
    except ModuleNotFoundError as exc:
        pytest.fail(f"device_grant_service missing (expected T6 RED): {exc}")
    return issue_device_grant, verify_device_grant


def _issue(now=1_000):
    issue, _verify = _functions()
    return issue(
        SECRET,
        DEVICE,
        "terminal",
        ARGS,
        now=now,
        ttl_seconds=30,
        nonce="nonce-fixed-for-test",
    )


def test_issue_and_verify_with_canonical_key_order():
    grant = _issue()
    _issue_fn, verify = _functions()
    assert grant["version"] == 1
    assert grant["nonce"] == "nonce-fixed-for-test"
    assert grant["exp"] == 1_030
    assert len(grant["signature"]) == 64
    reordered = {"nested": {"a": 1, "b": 2}, "command": "uname -a"}
    assert verify(SECRET, DEVICE, "terminal", reordered, grant, now=1_001) is True


def test_tampered_args_are_rejected():
    grant = _issue()
    _issue_fn, verify = _functions()
    tampered = {**ARGS, "command": "echo changed"
    }
    assert verify(SECRET, DEVICE, "terminal", tampered, grant, now=1_001) is False


def test_wrong_secret_device_and_tool_are_rejected():
    grant = _issue()
    _issue_fn, verify = _functions()
    assert verify("wrong-secret", DEVICE, "terminal", ARGS, grant, now=1_001) is False
    assert verify(SECRET, "other-device", "terminal", ARGS, grant, now=1_001) is False
    assert verify(SECRET, DEVICE, "process", ARGS, grant, now=1_001) is False


def test_expiry_is_closed_at_exp():
    grant = _issue()
    _issue_fn, verify = _functions()
    assert verify(SECRET, DEVICE, "terminal", ARGS, grant, now=1_029) is True
    assert verify(SECRET, DEVICE, "terminal", ARGS, grant, now=1_030) is False


def test_replayed_nonce_is_rejected_after_consumption():
    grant = _issue()
    _issue_fn, verify = _functions()
    consumed = set()
    assert verify(
        SECRET, DEVICE, "terminal", ARGS, grant, now=1_001,
        consumed_nonces=consumed,
    ) is True
    # The client ledger consumes atomically before execution; this test models
    # the state after that consume without calling two stateless verifies a
    # replay-defense proof by themselves.
    consumed.add(grant["nonce"])
    assert verify(
        SECRET, DEVICE, "terminal", ARGS, grant, now=1_001,
        consumed_nonces=consumed,
    ) is False


def test_malformed_or_tampered_signature_is_rejected():
    grant = _issue()
    _issue_fn, verify = _functions()
    bad = {**grant, "signature": "0" * 64}
    assert verify(SECRET, DEVICE, "terminal", ARGS, bad, now=1_001) is False
    assert verify(SECRET, DEVICE, "terminal", ARGS, None, now=1_001) is False


def test_session_secret_is_random_and_rotates_on_reconnect():
    manager = DeviceSessionManager()
    first = manager.register(DEVICE, "u1", object())
    first_secret = getattr(first, "pairing_secret", None)
    assert isinstance(first_secret, str) and len(first_secret) >= 32

    second = manager.register(DEVICE, "u1", object())
    second_secret = getattr(second, "pairing_secret", None)
    assert isinstance(second_secret, str) and second_secret != first_secret
    lookup = getattr(manager, "pairing_secret_for", None)
    assert callable(lookup)
    assert lookup(DEVICE) == second_secret


class _FakeWS:
    def __init__(self):
        self.headers = {"authorization": "Bearer device-test-token"}
        self.sent = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive_json(self):
        from fastapi import WebSocketDisconnect

        raise WebSocketDisconnect()

    async def close(self, code=1000, reason=""):
        return None


class _FakeDB:
    async def execute(self, _statement):
        return None

    async def commit(self):
        return None


def test_ws_session_ready_discloses_current_secret_after_ownership_recheck(monkeypatch):
    from app.api.device_ws import device_exec_ws
    from app.services import client_device_service as cds

    device = SimpleNamespace(device_id=DEVICE, user_id="u1")

    async def fake_verify(_db, _token):
        return device

    monkeypatch.setattr(cds, "verify_device_token", fake_verify)
    ws = _FakeWS()
    asyncio.run(device_exec_ws(ws, db=_FakeDB()))

    assert ws.accepted is True
    assert len(ws.sent) == 1
    ready = ws.sent[0]
    assert ready["type"] == "session_ready"
    assert ready["grant"]["version"] == 1
    assert ready["grant"]["algorithm"] == "HMAC-SHA256"
    assert len(ready["grant"]["pairing_secret"]) >= 32
    assert "sort_keys" in ready["grant"]["canonical_args"]
