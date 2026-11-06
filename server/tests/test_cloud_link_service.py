"""W32 Task 2 — 云端链路服务（中继凭据/令牌/请求面）。

Consistency Hub：
- 凭据源 = SyncState{server_url, cloud_username, cloud_password}（本机同盒 D-13）
- `CloudLinkService.request(user_id, method, path, *, json=None, params=None,
  timeout=30.0, stream=False) -> httpx.Response`（自动登录；401 重登一次重试）
- `is_linked(user_id) -> bool` · `ensure_conversation(user_id, conv_id) -> bool`
- fail-closed：无绑定 → LinkUnavailable；凭据错 → LinkAuthError；不假成功。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_cloud_link_service.py -v
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.services import cloud_link_service as cls
from app.services.cloud_link_service import (
    CloudLinkService,
    LinkAuthError,
    LinkUnavailable,
)


class FakeBinding:
    server_url = "https://cloud.example"
    cloud_username = "alice"
    cloud_password = "pw"


def _wire(monkeypatch, handler, binding=FakeBinding()):
    monkeypatch.setattr(cls, "_load_sync_binding", lambda user_id: binding)
    monkeypatch.setattr(cls, "_wake_sync", lambda: None)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="")
    monkeypatch.setattr(cls, "_get_client", lambda: client)
    return client


def test_request_logs_in_and_sends_bearer(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers.get("authorization")))
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"access_token": "tok-1"})
        return httpx.Response(200, json={"devices": []})

    _wire(monkeypatch, handler)
    resp = asyncio.run(CloudLinkService().request("u1", "GET", "/api/devices"))
    assert resp.status_code == 200
    assert ("/api/auth/login", None) in calls
    assert ("/api/devices", "Bearer tok-1") in calls


def test_request_401_relogins_once_and_retries(monkeypatch):
    state = {"n": 0, "logins": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            state["logins"] += 1
            return httpx.Response(200, json={"access_token": f"tok-{state['logins']}"})
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(401)
        assert request.headers.get("authorization") == "Bearer tok-2"
        return httpx.Response(200, json={"ok": True})

    _wire(monkeypatch, handler)
    resp = asyncio.run(CloudLinkService().request("u1", "GET", "/api/devices"))
    assert resp.status_code == 200 and state["logins"] == 2


def test_request_without_binding_raises(monkeypatch):
    monkeypatch.setattr(cls, "_load_sync_binding", lambda user_id: None)
    try:
        asyncio.run(CloudLinkService().request("u1", "GET", "/api/devices"))
    except LinkUnavailable:
        pass
    else:
        raise AssertionError("无云端绑定必须 raise LinkUnavailable（fail-closed，不假成功）")


def test_bad_credentials_raise_link_auth_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    _wire(monkeypatch, handler)
    try:
        asyncio.run(CloudLinkService().request("u1", "GET", "/api/devices"))
    except LinkAuthError:
        pass
    else:
        raise AssertionError("登录失败必须 raise LinkAuthError（不假成功）")


def test_is_linked(monkeypatch):
    monkeypatch.setattr(cls, "_load_sync_binding", lambda user_id: FakeBinding())
    assert asyncio.run(CloudLinkService().is_linked("u1")) is True
    monkeypatch.setattr(cls, "_load_sync_binding", lambda user_id: None)
    assert asyncio.run(CloudLinkService().is_linked("u1")) is False


def test_ensure_conversation_wakes_sync_then_polls(monkeypatch):
    state = {"gets": 0, "wakes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"access_token": "tok-1"})
        state["gets"] += 1
        if state["gets"] == 1:
            return httpx.Response(404)  # 云端尚未有该会话
        return httpx.Response(200, json={"id": "conv-1"})

    def wake():
        state["wakes"] += 1

    _wire(monkeypatch, handler)
    monkeypatch.setattr(cls, "_wake_sync", wake)
    monkeypatch.setattr(cls, "_poll_sleep", lambda s: asyncio.sleep(0))
    ok = asyncio.run(CloudLinkService().ensure_conversation("u1", "conv-1"))
    assert ok is True and state["wakes"] >= 1 and state["gets"] >= 2


def test_cloud_binding_carries_tls_verify_and_selects_insecure_client(monkeypatch):
    b = cls.CloudBinding("https://60.example", "u", "p", False)
    assert b.tls_verify is False
    sentinel = object()
    monkeypatch.setattr(cls, "_insecure_client", sentinel)
    monkeypatch.setattr(cls, "_get_client", lambda: (_ for _ in ()).throw(AssertionError("verify client used")))
    assert cls._client_for(b) is sentinel
