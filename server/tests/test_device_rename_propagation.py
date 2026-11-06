"""W25 设备显示名云端同步（用户报障：本地改了显示名称，云端没有改）。

覆盖（A4.8，RED→GREEN）：
  R1 云端 exec 注册表改名端点（handoff ③ 形状）：POST /api/devices/{device_id}/rename
     凭 device token → 200 + 注册表 name 更新（list 反映）。
  R2 坏 token 401/403；非本设备 token 403/404。
  R3 本地「保存名称」推送云端：rename 自动调云端改名端点（假云端记录请求），
     renamed_cloud=true。
  R4 旧版云端 404/不可达 → renamed_cloud=false + cloud_note 如实；本地改名不回滚。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_device_rename_propagation.py -v
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_device_exec_api import _fresh_http  # noqa: F401,E402
from test_client_exec_rename import _fake_identity_db  # noqa: F401,E402
from test_client_exec_rename import _client as _rename_client  # noqa: E402
from test_client_exec_rename import _seed_binding  # noqa: E402

from app.core.config import get_config  # noqa: E402

# ---------------- 假云端（记录改名请求；可切 404/网络失败模式） ----------------


class _FakeCloudHandler(BaseHTTPRequestHandler):
    """POST /api/devices/{device_id}/rename：记录 path/headers/body；mode 控响应。"""

    def log_message(self, *args):
        pass

    received = []
    mode = "ok"  # ok | 404 | 500

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).received.append({
            "path": self.path,
            "auth": self.headers.get("Authorization") or "",
            "body": dict(body),
        })
        if type(self).mode == "404":
            data = json.dumps({"detail": "Not Found"}).encode()
            self.send_response(404)
        elif type(self).mode == "500":
            data = b"boom"
            self.send_response(500)
        elif type(self).mode == "html200":
            data = b"<!DOCTYPE html><html><body>fallback</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        else:
            data = json.dumps({"device_id": self.path.split("/")[3], "name": body.get("name"), "renamed": True}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def fake_cloud():
    _FakeCloudHandler.received = []
    _FakeCloudHandler.mode = "ok"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCloudHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


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


# ---------------- 云端面（handoff ③ 端点形状） ----------------

def _pair_device(db, user, *, name="旧名字", device_id="dev-rename01"):
    from app.services import client_device_service as cds
    rec = cds.issue_pairing_code(user.id)
    import asyncio
    row, dev_token = asyncio.run(cds.register_device(db, code=rec["code"], name=name, platform="macos", device_id=device_id))
    return row, dev_token


def _devices_client(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import client_devices
    from app.core.deps import get_db

    app = FastAPI()
    app.include_router(client_devices.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_cloud_rename_endpoint_updates_registry(_fake_identity_db):
    sess, db, user = _fresh_http()
    row, dev_token = _pair_device(db, user)
    client = _devices_client(db)
    r = client.post(f"/api/devices/{row.device_id}/rename",
                    json={"name": "新名字"},
                    headers={"Authorization": f"Bearer {dev_token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["renamed"] is True and body["name"] == "新名字"
    sess.expire_all()
    from sqlalchemy import select
    from app.db.database import ClientExecutorDevice
    fresh = sess.execute(select(ClientExecutorDevice).where(ClientExecutorDevice.device_id == row.device_id)).scalar_one()
    assert fresh.name == "新名字"


def test_cloud_rename_rejects_bad_or_foreign_token(_fake_identity_db):
    sess, db, user = _fresh_http()
    row, dev_token = _pair_device(db, user, device_id="dev-rename02")
    other, foreign_token = _pair_device(db, user, device_id="dev-rename03")
    client = _devices_client(db)
    r1 = client.post(f"/api/devices/{row.device_id}/rename", json={"name": "x"},
                     headers={"Authorization": "Bearer wrong-token"})
    assert r1.status_code in (401, 403), r1.text
    r2 = client.post(f"/api/devices/{row.device_id}/rename", json={"name": "x"},
                     headers={"Authorization": f"Bearer {foreign_token}"})
    assert r2.status_code in (403, 404), r2.text
    r3 = client.post(f"/api/devices/{row.device_id}/rename", json={"name": ""})
    assert r3.status_code in (400, 401), r3.text


# ---------------- 本地面（rename 推送链） ----------------

def test_local_rename_pushes_to_cloud(store_env, fake_cloud):
    _seed_binding(store_env, {
        "cloud_url": fake_cloud, "device_id": "dev-1001", "name": "old-name",
        "platform": "macos", "token": "dummy-test-token-1001", "tls_verify": True,
        "created_at": "2026-10-15T00:00:00+00:00", "local_user_id": "u1",
    })
    client = _rename_client()
    r = client.post("/api/client-exec/rename", json={"name": "新名字"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["renamed"] is True
    assert body["renamed_cloud"] is True, body
    assert _FakeCloudHandler.received, "云端未收到改名请求"
    req = _FakeCloudHandler.received[-1]
    assert req["path"] == "/api/devices/dev-1001/rename"
    assert req["body"].get("name") == "新名字"
    assert "dummy-test-token-1001" in req["auth"]


def test_local_rename_falls_back_on_old_cloud(store_env, fake_cloud):
    _FakeCloudHandler.mode = "404"
    _seed_binding(store_env, {
        "cloud_url": fake_cloud, "device_id": "dev-1001", "name": "old-name",
        "platform": "macos", "token": "dummy-test-token-1001", "tls_verify": True,
        "created_at": "2026-10-15T00:00:00+00:00", "local_user_id": "u1",
    })
    client = _rename_client()
    r = client.post("/api/client-exec/rename", json={"name": "新名字"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["renamed"] is True
    assert body["renamed_cloud"] is False, body
    assert body.get("cloud_note"), "须如实给出云端未同步原因"
    # 本地改名不回滚
    import json as _json
    rec = _json.loads((store_env / "app" / "device_exec.json").read_text(encoding="utf-8"))
    assert rec["name"] == "新名字"


# ---------------- 评审收口腿（r1/r2 Minor：鉴权负面+伪 200+超长） ----------------

def test_cloud_rename_rejects_user_jwt_and_revoked(_fake_identity_db):
    """用户 JWT 不得冒充 device token；revoked token 一律拒（回归钉）。"""
    sess, db, user = _fresh_http()
    row, dev_token = _pair_device(db, user, device_id="dev-rename04")
    client = _devices_client(db)
    r_jwt = client.post(f"/api/devices/{row.device_id}/rename", json={"name": "x"},
                        headers={"Authorization": "Bearer user-jwt-not-a-device-token"})
    assert r_jwt.status_code == 401, r_jwt.text
    # 撤销后 token 立即失效
    row.revoked = True
    sess.commit()
    r_rev = client.post(f"/api/devices/{row.device_id}/rename", json={"name": "x"},
                        headers={"Authorization": f"Bearer {dev_token}"})
    assert r_rev.status_code == 401, r_rev.text


def test_cloud_rename_empty_and_oversize_name_with_auth(_fake_identity_db):
    sess, db, user = _fresh_http()
    row, dev_token = _pair_device(db, user, device_id="dev-rename05")
    client = _devices_client(db)
    r_empty = client.post(f"/api/devices/{row.device_id}/rename", json={"name": "  "},
                          headers={"Authorization": f"Bearer {dev_token}"})
    assert r_empty.status_code == 400, r_empty.text
    r_long = client.post(f"/api/devices/{row.device_id}/rename", json={"name": "x" * 256},
                         headers={"Authorization": f"Bearer {dev_token}"})
    assert r_long.status_code == 400 and "超长" in r_long.text, r_long.text
    r_missing = client.post(f"/api/devices/{row.device_id}/rename", json={},
                            headers={"Authorization": f"Bearer {dev_token}"})
    assert r_missing.status_code == 400, r_missing.text


def test_local_rename_garbage_200_not_fabricated(store_env, fake_cloud):
    """评审 I1：200+HTML/空体伪应答不得虚报「云端已同步」。"""
    _FakeCloudHandler.mode = "html200"
    _seed_binding(store_env, {
        "cloud_url": fake_cloud, "device_id": "dev-1001", "name": "old-name",
        "platform": "macos", "token": "dummy-test-token-1001", "tls_verify": True,
        "created_at": "2026-10-15T00:00:00+00:00", "local_user_id": "u1",
    })
    client = _rename_client()
    r = client.post("/api/client-exec/rename", json={"name": "新名字"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["renamed"] is True
    assert body["renamed_cloud"] is False, body
    assert "未确认改名" in body["cloud_note"] or "异常" in body["cloud_note"]


def test_local_rename_http500_honest_note(store_env, fake_cloud):
    _FakeCloudHandler.mode = "500"
    _seed_binding(store_env, {
        "cloud_url": fake_cloud, "device_id": "dev-1001", "name": "old-name",
        "platform": "macos", "token": "dummy-test-token-1001", "tls_verify": True,
        "created_at": "2026-10-15T00:00:00+00:00", "local_user_id": "u1",
    })
    client = _rename_client()
    r = client.post("/api/client-exec/rename", json={"name": "新名字"})
    body = r.json()
    assert body["renamed_cloud"] is False
    assert "HTTP 500" in body["cloud_note"], body
