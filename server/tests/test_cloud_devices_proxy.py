"""W32 Task 3 — 云端执行器注册表代理端点（契约=server/API.md「Cloud Device Registry」节）。

行为面（自 API.md 提炼）：
- GET /api/client-exec/cloud-devices：以云端账户身份代理云端 GET /api/devices；
  裁剪字段 {device_id,name,platform,online,revoked,last_seen_at,is_self}；不含 token。
  未绑定云端 → 200 {"linked": false, "devices": []}；云端不可达 → 500（含「云端不可达」）；
  云端凭据失效 → 400（含「重新绑定」）。
- POST /api/client-exec/cloud-devices/{device_id}/revoke：代理云端 revoke；
  本机设备拒绝（400 含「不能撤销本机设备」）；云端拒绝透传 detail。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_cloud_devices_proxy.py -v
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _FakeCloudHandler(BaseHTTPRequestHandler):
    """假云端：POST /api/auth/login → token；GET /api/devices → 注册表；
    POST /api/devices/{id}/revoke → 撤销回执。行为可按类属性切换。"""

    def log_message(self, *args):
        pass

    login_status = 200
    devices_status = 200
    revoke_status = 200
    devices_payload = [
        {"device_id": "dev-self", "name": "我的Mac", "platform": "macos",
         "online": True, "revoked": False, "last_seen_at": "2026-10-28T00:00:00Z"},
        {"device_id": "dev-other", "name": "笔记本", "platform": "linux",
         "online": False, "revoked": False, "last_seen_at": "2026-10-27T00:00:00Z"},
    ]
    calls = []

    def do_POST(self):
        type(self).calls.append(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        _ = self.rfile.read(length or 0)
        if self.path == "/api/auth/login":
            status = type(self).login_status
            payload = {"access_token": "cloud-tok"} if status == 200 else {"detail": "bad creds"}
            self._reply(status, payload)
            return
        if self.path.endswith("/revoke"):
            self._reply(type(self).revoke_status, {"revoked": True})
            return
        self._reply(404, {"detail": "not found"})

    def do_GET(self):
        type(self).calls.append(self.path)
        if self.path == "/api/devices":
            self._reply(type(self).devices_status, type(self).devices_payload)
            return
        self._reply(404, {"detail": "not found"})

    def _reply(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def fake_cloud():
    _FakeCloudHandler.calls = []
    _FakeCloudHandler.login_status = 200
    _FakeCloudHandler.devices_status = 200
    _FakeCloudHandler.revoke_status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCloudHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _fake_identity_db(monkeypatch):
    """cloud_link/_name_gate 类 DB 会话走桩——TestClient 短命事件循环不毒化全局池。"""
    class _DB:
        async def get(self, model, key):
            return None

        async def execute(self, stmt):
            class _R:
                def scalars(self):
                    class _S:
                        def first(self):
                            return None
                    return _S()
            return _R()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    import app.db.database as dbmod
    monkeypatch.setattr(dbmod, "AsyncSessionLocal", lambda: _DB())


def _wire_link(monkeypatch, base_url):
    from app.services import cloud_link_service as cls

    class _B:
        server_url = base_url
        cloud_username = "alice"
        cloud_password = "pw"

    monkeypatch.setattr(cls, "_load_sync_binding", lambda user_id: _B())
    monkeypatch.setattr(cls, "_wake_sync", lambda: None)
    # 隔离缝：单例令牌缓存跨用例泄漏（iter 7 diagnosis）——每用例清空
    cls.cloud_link_service._tokens.clear()


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import client_exec as client_exec_api
    from app.core.deps import get_current_user

    app = FastAPI()
    app.include_router(client_exec_api.router)
    app.dependency_overrides[get_current_user] = lambda: type(
        "U", (), {"id": "u1", "username": "tester"})()
    return TestClient(app)


def test_cloud_devices_lists_registry_with_is_self(fake_cloud, monkeypatch):
    _wire_link(monkeypatch, fake_cloud)
    monkeypatch.setattr(
        "app.services.device_pairing_service.load_binding",
        lambda: {"device_id": "dev-self", "token": "t"},
    )
    client = _client()
    r = client.get("/api/client-exec/cloud-devices")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["linked"] is True
    assert len(body["devices"]) == 2
    by_id = {d["device_id"]: d for d in body["devices"]}
    assert by_id["dev-self"]["is_self"] is True
    assert by_id["dev-other"]["is_self"] is False
    assert by_id["dev-self"]["name"] == "我的Mac"
    assert "token" not in json.dumps(body), "注册表代理不得泄 token"


def test_cloud_devices_unlinked_returns_empty(monkeypatch):
    from app.services import cloud_link_service as cls
    monkeypatch.setattr(cls, "_load_sync_binding", lambda user_id: None)
    monkeypatch.setattr(cls, "_load_sync_binding_async", None)  # 占位防误用
    client = _client()

    async def _no_async(user_id):
        return None

    monkeypatch.setattr(cls, "_load_sync_binding_async", _no_async)
    r = client.get("/api/client-exec/cloud-devices")
    assert r.status_code == 200, r.text
    assert r.json() == {"linked": False, "devices": []}


def test_cloud_devices_cloud_unreachable_500(monkeypatch):
    _wire_link(monkeypatch, "http://127.0.0.1:1")  # 死端口
    client = _client()
    r = client.get("/api/client-exec/cloud-devices")
    assert r.status_code == 500, r.text
    assert "云端不可达" in r.json()["detail"]


def test_cloud_devices_cloud_auth_error_400(fake_cloud, monkeypatch):
    _FakeCloudHandler.login_status = 401
    _wire_link(monkeypatch, fake_cloud)
    client = _client()
    r = client.get("/api/client-exec/cloud-devices")
    assert r.status_code == 400, r.text
    assert "重新绑定" in r.json()["detail"]


def test_cloud_revoke_proxies_to_cloud(fake_cloud, monkeypatch):
    _wire_link(monkeypatch, fake_cloud)
    monkeypatch.setattr(
        "app.services.device_pairing_service.load_binding",
        lambda: {"device_id": "dev-self", "token": "t"},
    )
    client = _client()
    r = client.post("/api/client-exec/cloud-devices/dev-other/revoke")
    assert r.status_code == 200, r.text
    assert r.json() == {"revoked": True}
    assert "/api/devices/dev-other/revoke" in _FakeCloudHandler.calls


def test_cloud_revoke_self_rejected(fake_cloud, monkeypatch):
    _wire_link(monkeypatch, fake_cloud)
    monkeypatch.setattr(
        "app.services.device_pairing_service.load_binding",
        lambda: {"device_id": "dev-self", "token": "t"},
    )
    client = _client()
    r = client.post("/api/client-exec/cloud-devices/dev-self/revoke")
    assert r.status_code == 400, r.text
    assert "不能撤销本机设备" in r.json()["detail"]
    assert not any(c.endswith("/revoke") for c in _FakeCloudHandler.calls), "本机设备不得透传云端"


def test_cloud_revoke_translates_cloud_rejection(fake_cloud, monkeypatch):
    _FakeCloudHandler.revoke_status = 404
    _wire_link(monkeypatch, fake_cloud)
    monkeypatch.setattr(
        "app.services.device_pairing_service.load_binding",
        lambda: {"device_id": "dev-self", "token": "t"},
    )
    client = _client()
    r = client.post("/api/client-exec/cloud-devices/dev-other/revoke")
    assert r.status_code == 400, r.text
    assert r.json()["detail"]
