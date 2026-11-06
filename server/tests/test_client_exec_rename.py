"""设备改名 + 重配对名称保持（acceptance #22/#24/#25；契约=server/API.md「设备执行器」节）。

接缝与 test_client_exec_pairing.py 同款：LOCALSTACK_HOME 临时目录种子绑定记录 +
FastAPI TestClient（真实路由/依赖覆盖）。rename=仅本地绑定记录 name 更新——上游
client_devices.py 无设备改名端点（handoff 缺口），不虚构云端调用。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_client_exec_rename.py -v
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import get_config  # noqa: E402


class _FakeCloudHandler(BaseHTTPRequestHandler):
    """假云端 POST /api/devices/pair：任意码换 token，回显请求 name。"""

    def log_message(self, *args):
        pass

    last_body = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).last_body = dict(body)
        payload = {
            "device_id": body.get("device_id") or "dev-1001",
            "token": "dummy-test-token-1001",
            "name": body.get("name") or "cloud-echo-name",
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def fake_cloud():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCloudHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _fake_identity_db(monkeypatch):
    """status 端点 _name_gate_ready 会开 DB 会话——TestClient 短命事件循环上
    真引擎连接会毒化全局池（test_client_exec_pairing 同款防护）。"""
    from types import SimpleNamespace as NS  # noqa: F401

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


@pytest.fixture()
def store_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALSTACK_HOME", str(tmp_path))
    monkeypatch.delenv("WEAVE_LOCALSTACK_HOME", raising=False)
    config = get_config()
    saved = config._config.get("client_exec")
    config._config["client_exec"] = {"enabled": True, "cloud_url": "", "heartbeat_seconds": 5,
                                     "call_timeout_seconds": 120, "max_payload_mb": 10,
                                     "allowed_tools": ["terminal"]}
    yield tmp_path
    if saved is None:
        config._config.pop("client_exec", None)
    else:
        config._config["client_exec"] = saved


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import client_exec as client_exec_api
    from app.core.deps import get_current_user

    app = FastAPI()
    app.include_router(client_exec_api.router)
    app.dependency_overrides[get_current_user] = lambda: type("U", (), {"id": "u1", "username": "tester"})()
    return TestClient(app)


def _seed_binding(store_env: Path, record: dict) -> Path:
    store = store_env / "app" / "device_exec.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.chmod(store, 0o600)
    return store


# ---------------- rename（API.md「Rename Device」）----------------

def test_rename_updates_binding_name_and_status(store_env):
    _seed_binding(store_env, {
        "cloud_url": "http://127.0.0.1:9", "device_id": "dev-1001", "name": "old-name",
        "platform": "macos", "token": "dummy-test-token-1001", "tls_verify": True,
        "created_at": "2026-10-14T00:00:00+00:00", "local_user_id": "u1",
    })
    client = _client()
    r = client.post("/api/client-exec/rename", json={"name": "新名字"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["renamed"] is True
    assert body["name"] == "新名字"
    assert body["device_id"] == "dev-1001"

    record = json.loads((store_env / "app" / "device_exec.json").read_text(encoding="utf-8"))
    assert record["name"] == "新名字", "绑定记录 name 必须更新"
    assert record["token"] == "dummy-test-token-1001", "改名不得动 token"

    st = client.get("/api/client-exec/status")
    assert st.status_code == 200, st.text
    assert st.json()["name"] == "新名字", "status 面须展示改名后名称"


def test_rename_rejects_empty_or_blank_name(store_env):
    _seed_binding(store_env, {
        "cloud_url": "http://127.0.0.1:9", "device_id": "dev-1001", "name": "old-name",
        "platform": "macos", "token": "dummy-test-token-1001", "tls_verify": True,
        "created_at": "2026-10-14T00:00:00+00:00", "local_user_id": "u1",
    })
    client = _client()
    for payload in ({"name": ""}, {"name": "   "}, {}):
        r = client.post("/api/client-exec/rename", json=payload)
        assert r.status_code == 400, (payload, r.status_code, r.text)
    record = json.loads((store_env / "app" / "device_exec.json").read_text(encoding="utf-8"))
    assert record["name"] == "old-name", "拒绝的改名不得落盘"


def test_rename_requires_paired_token(store_env):
    client = _client()
    r = client.post("/api/client-exec/rename", json={"name": "x"})
    assert r.status_code == 400, "无记录（未配对）→ 400"
    assert "未配对" in r.json()["detail"]

    _seed_binding(store_env, {  # reset 墓碑：无 token
        "cloud_url": "http://127.0.0.1:9", "device_id": "dev-1001", "name": "old-name",
        "platform": "macos", "tls_verify": True, "created_at": "2026-10-14T00:00:00+00:00",
    })
    r = client.post("/api/client-exec/rename", json={"name": "x"})
    assert r.status_code == 400, "墓碑（无 token）→ 400"
    assert "未配对" in r.json()["detail"]


# ---------------- 重配对名称保持（#24 后端腿）----------------

def test_repair_with_empty_name_keeps_previous_name(store_env, fake_cloud):
    client = _client()
    r1 = client.post("/api/client-exec/pair", json={
        "pairing_code": "CODE-A", "cloud_url": fake_cloud, "name": "my-dev",
    })
    assert r1.status_code == 200, r1.text

    r2 = client.post("/api/client-exec/pair", json={
        "pairing_code": "CODE-B", "cloud_url": fake_cloud, "name": "",
    })
    assert r2.status_code == 200, r2.text
    assert r2.json()["name"] == "my-dev", "重配对空名须回落旧绑定名（不得变 cloud-echo-name/device）"

    record = json.loads((store_env / "app" / "device_exec.json").read_text(encoding="utf-8"))
    assert record["name"] == "my-dev"


# ---------------- 换代标注真相（#24 换代标注保持）----------------

class _FreshIdCloudHandler(BaseHTTPRequestHandler):
    """假云端 B：请求不带 device_id 时发新代 id（跨云/逃生口语义）。"""

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        payload = {
            "device_id": body.get("device_id") or "dev-2002",
            "token": "dummy-test-token-2002",
            "name": body.get("name") or "cloud-echo-name",
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_cross_cloud_pair_flags_device_id_changed(store_env, fake_cloud):
    """换云重配对 = 换代：device_id_changed 必须为真（前端据实提示，不得假报「保持连续」）。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FreshIdCloudHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    cloud_b = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        client = _client()
        r1 = client.post("/api/client-exec/pair", json={
            "pairing_code": "CODE-A", "cloud_url": fake_cloud, "name": "my-dev",
        })
        assert r1.status_code == 200, r1.text
        assert r1.json()["device_id"] == "dev-1001"

        r2 = client.post("/api/client-exec/pair", json={
            "pairing_code": "CODE-B", "cloud_url": cloud_b, "name": "",
        })
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["device_id"] == "dev-2002", "换云不得携带旧云 device_id"
        assert body.get("device_id_changed") is True, f"换代必须标注（wire 真相：dev-1001→dev-2002）got={body}"
    finally:
        srv.shutdown()
        thread.join(timeout=5)
