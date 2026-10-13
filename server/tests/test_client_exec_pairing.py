"""配对/凭据 + 本地面板 API（PLAN_device_executor §1.2；端点契约=server/API.md「设备执行器」节）。

假云端 HTTP 端为真实 ThreadingHTTPServer（收 POST /api/devices）；REST 面走
FastAPI TestClient（真实路由/依赖注入）。断言：0600 落盘、token 不回传不落日志、
错码/过期码零落盘、reset 清凭据断连。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_client_exec_pairing.py -v
"""
import asyncio
import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import get_config  # noqa: E402


class _FakeCloudHandler(BaseHTTPRequestHandler):
    """假云端 POST /api/devices：好码换 token；过期码 410；错码 400。"""

    def log_message(self, *args):  # 静音
        pass

    last_body = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).last_body = dict(body)
        if self.path != "/api/devices/pair":
            self._reply(404, {"detail": "not found"})
            return
        code = str(body.get("code") or "")  # W16b：wire 字段名为 code
        if code == "GOOD-CODE-1":
            payload = {  # W16b DevicePairResponse 契约：{device_id, token, name?}
                # device_id echo（handoff 后上游语义：同 id 重注册=同行换 token 保身份）
                "device_id": body.get("device_id") or "dev-0001",
                "token": "fake-test-token-0000",
                "name": body.get("name") or "cloud-name",
            }
            self._reply(200, payload)
        elif code == "GOOD-CODE-2":
            payload = {  # W20：上游 pair 响应携带云端身份（handoff 字段）
                "device_id": body.get("device_id") or "dev-0002",
                "token": "fake-test-token-0002",
                "name": "cloud-name",
                "user_id": "cafef00d-0000-4000-8000-000000000000",
                "username": "alice",
            }
            self._reply(200, payload)
        elif code == "EXPIRED-CODE":
            self._reply(410, {"detail": "pairing code expired"})
        else:
            self._reply(400, {"detail": "invalid pairing code"})

    def _reply(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
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
    """W20：status 端点 _name_gate_ready 会开 DB 会话——本文件测试跑在 TestClient
    短命事件循环上，真引擎连接会滞留全局池并毒化后续 asyncio.run 真库测试
    （poll_pending「Future attached to a different loop」实证）。统一假件化。"""
    from types import SimpleNamespace as NS

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


# ---------------- 配对流程（API.md「Pair Device」）----------------

def test_pair_saves_token_0600_and_never_returns_it(store_env, fake_cloud):
    client = _client()
    r = client.post("/api/client-exec/pair", json={
        "pairing_code": "GOOD-CODE-1", "cloud_url": fake_cloud, "name": "my-dev",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["device_id"] == "dev-0001"
    assert "token" not in body and "fake-test-token" not in r.text, "token 不得回传前端"

    store = store_env / "app" / "device_exec.json"
    assert store.is_file()
    mode = stat.S_IMODE(store.stat().st_mode)
    assert mode == 0o600, f"凭据文件必须 0600，实际 {oct(mode)}"
    record = json.loads(store.read_text(encoding="utf-8"))
    assert record["token"] == "fake-test-token-0000"
    assert record["cloud_url"] == fake_cloud
    assert record["device_id"] == "dev-0001"


def test_pair_wrong_or_expired_code_rejected_zero_disk(store_env, fake_cloud):
    client = _client()
    for code, expect in (("BAD-CODE", 400), ("EXPIRED-CODE", 410)):
        r = client.post("/api/client-exec/pair", json={"pairing_code": code, "cloud_url": fake_cloud})
        assert r.status_code == expect, (code, r.status_code, r.text)
    assert not (store_env / "app" / "device_exec.json").exists(), "错码/过期码不得落盘"


def test_pair_requires_code(store_env, fake_cloud):
    client = _client()
    r = client.post("/api/client-exec/pair", json={"cloud_url": fake_cloud})
    assert r.status_code == 400, r.text


# ---------------- 状态与 reset（API.md）----------------

def test_status_shape_and_no_token(store_env, fake_cloud):
    client = _client()
    r = client.get("/api/client-exec/status")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("enabled", "paired", "connected", "cloud_url", "last_heartbeat_at", "last_error"):
        assert key in body, key
    assert body["paired"] is False
    assert "token" not in body

    client.post("/api/client-exec/pair", json={"pairing_code": "GOOD-CODE-1", "cloud_url": fake_cloud})
    body = client.get("/api/client-exec/status").json()
    assert body["paired"] is True and body["device_id"] == "dev-0001"
    assert "token" not in body


def test_reset_clears_credential(store_env, fake_cloud):
    """契约改形（P0#3，- test-change 登记）：reset 不再删文件——只清 token 留墓碑
    （device_id/身份连续性，见 test_reset_keeps_device_id_tombstone）。"""
    client = _client()
    client.post("/api/client-exec/pair", json={"pairing_code": "GOOD-CODE-1", "cloud_url": fake_cloud})
    r = client.post("/api/client-exec/reset")
    assert r.status_code == 200 and r.json()["reset"] is True, r.text
    path = store_env / "app" / "device_exec.json"
    assert path.exists(), "墓碑留存（只清 token）"
    assert not json.loads(path.read_text(encoding="utf-8")).get("token")
    r = client.post("/api/client-exec/reset")
    assert r.json()["reset"] is False, "无 token 时 reset 幂等 false"


def test_pair_upstream_error_passthrough(store_env, fake_cloud):
    """云端 5xx → 502 网关语义（不吞状态码细节）且零落盘。"""
    client = _client()
    # 云端不可达：指向关闭端口
    r = client.post("/api/client-exec/pair", json={"pairing_code": "GOOD-CODE-1",
                                                   "cloud_url": "http://127.0.0.1:1"})
    assert r.status_code == 502, r.text
    assert not (store_env / "app" / "device_exec.json").exists()


def test_token_never_in_logs(store_env, fake_cloud, caplog):
    """token 不进日志（判据 #9 子腿：捕获日志断言缺失性）。"""
    import logging

    caplog.set_level(logging.DEBUG)
    client = _client()
    r = client.post("/api/client-exec/pair", json={"pairing_code": "GOOD-CODE-1", "cloud_url": fake_cloud})
    assert r.status_code == 200
    assert "fake-test-token-0000" not in caplog.text, "device token 不得出现在任何日志记录"


# ---------------- W20 身份绑定（PLAN_device_workspace_identity_impl T2）----------------

def test_pair_records_local_user_id(store_env, fake_cloud):
    """配对必须记录本地属主（local_user_id=配对发起者）——设备执行身份解析的首选源。"""
    client = _client()
    r = client.post("/api/client-exec/pair",
                    json={"pairing_code": "GOOD-CODE-1", "cloud_url": fake_cloud})
    assert r.status_code == 200, r.text
    record = json.loads((store_env / "app" / "device_exec.json").read_text(encoding="utf-8"))
    assert record.get("local_user_id") == "u1", f"配对记录缺 local_user_id，实际 {record!r}"


def test_pair_records_cloud_identity_when_response_carries_it(store_env, fake_cloud):
    """pair 响应携带云端身份（上游 W20 handoff 字段）时必须持久化——名门期望值来源。"""
    client = _client()
    r = client.post("/api/client-exec/pair",
                    json={"pairing_code": "GOOD-CODE-2", "cloud_url": fake_cloud})
    assert r.status_code == 200, r.text
    record = json.loads((store_env / "app" / "device_exec.json").read_text(encoding="utf-8"))
    assert record.get("cloud_user_id") == "cafef00d-0000-4000-8000-000000000000", record
    assert record.get("cloud_username") == "alice", record


def test_status_exposes_identity_bound(store_env, fake_cloud):
    """status.identity_bound：未配对=False；配对带 local_user_id=True（面板提示驱动）。"""
    client = _client()
    body = client.get("/api/client-exec/status").json()
    assert body.get("identity_bound") is False, body
    client.post("/api/client-exec/pair",
                json={"pairing_code": "GOOD-CODE-1", "cloud_url": fake_cloud})
    body = client.get("/api/client-exec/status").json()
    assert body.get("identity_bound") is True, body


def test_status_exposes_name_gate_ready(store_env, fake_cloud):
    """r2-F2 收口：name_gate_ready=名门期望值是否可算（面板提示驱动，identity_bound 不够）。"""
    client = _client()
    body = client.get("/api/client-exec/status").json()
    assert body.get("name_gate_ready") is False, body
    # GOOD-CODE-1：仅 local_user_id（无云端身份/无 sync 绑定）→ 未就绪
    client.post("/api/client-exec/pair",
                json={"pairing_code": "GOOD-CODE-1", "cloud_url": fake_cloud})
    body = client.get("/api/client-exec/status").json()
    assert body.get("name_gate_ready") is False, body
    # GOOD-CODE-2：pair 响应带云端身份 → 就绪
    client.post("/api/client-exec/reset")
    client.post("/api/client-exec/pair",
                json={"pairing_code": "GOOD-CODE-2", "cloud_url": fake_cloud})
    body = client.get("/api/client-exec/status").json()
    assert body.get("name_gate_ready") is True, body


def test_name_gate_ready_true_via_sync_backfill(store_env, fake_cloud, monkeypatch):
    """复评 NEW-5/C：_name_gate_ready 的 SyncState-True 分支（假件常 None 盖住的面）。"""
    from types import SimpleNamespace as NS

    client = _client()
    client.post("/api/client-exec/pair",
                json={"pairing_code": "GOOD-CODE-1", "cloud_url": fake_cloud})
    assert client.get("/api/client-exec/status").json()["name_gate_ready"] is False

    class _DB:
        async def get(self, model, key):
            return NS(user_id="u1", cloud_user_id="cafef00d-0000-4000-8000-000000000000",
                      cloud_username="alice")

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
    body = client.get("/api/client-exec/status").json()
    assert body["name_gate_ready"] is True, body


# ---------------- device_id 连续性 + 构建世代（下游待修清单 P0#1-3/P1#4）----------------

def test_pair_body_carries_device_id(store_env, fake_cloud):
    """P0#1/#2：pair 请求 body 必须带 device_id（doc↔code 契约闭合）。"""
    _FakeCloudHandler.last_body = {}
    client = _client()
    r = client.post("/api/client-exec/pair", json={
        "pairing_code": "GOOD-CODE-1", "cloud_url": fake_cloud,
        "device_id": "dev-CONT-0001",
    })
    assert r.status_code == 200, r.text
    assert getattr(_FakeCloudHandler, "last_body", {}).get("device_id") == "dev-CONT-0001", (
        f"pair body 缺 device_id，实际 {getattr(_H, 'last_body', {})!r}")


def test_pair_reuses_old_binding_device_id(store_env, fake_cloud):
    """P0#1：req 未带 device_id 时复用旧绑定（含 reset 墓碑）的 device_id。"""
    client = _client()
    _FakeCloudHandler.last_body = {}
    r = client.post("/api/client-exec/pair",
                    json={"pairing_code": "GOOD-CODE-1", "cloud_url": fake_cloud})
    assert r.status_code == 200, r.text
    rec1 = json.loads((store_env / "app" / "device_exec.json").read_text(encoding="utf-8"))
    # 旧记录（本次假云端回 dev-0001）
    assert rec1.get("device_id"), rec1
    client.post("/api/client-exec/pair",
                json={"pairing_code": "GOOD-CODE-1", "cloud_url": fake_cloud})
    assert getattr(_FakeCloudHandler, "last_body", {}).get("device_id") == rec1["device_id"], (
        "重配对必须复用旧 device_id（历史会话 execution_target 连续性）")


def test_reset_keeps_device_id_tombstone(store_env, fake_cloud):
    """P0#3：reset 只清 token——device_id/身份键留存（墓碑），后续重配对不换代。"""
    client = _client()
    client.post("/api/client-exec/pair", json={
        "pairing_code": "GOOD-CODE-2", "cloud_url": fake_cloud,
        "device_id": "dev-KEEP-777",
    })
    r = client.post("/api/client-exec/reset")
    assert r.status_code == 200 and r.json()["reset"] is True, r.text
    path = store_env / "app" / "device_exec.json"
    assert path.exists(), "reset 后须留墓碑（只清 token）"
    tomb = json.loads(path.read_text(encoding="utf-8"))
    assert not tomb.get("token"), "token 必须清除"
    assert tomb.get("device_id") == "dev-KEEP-777", f"device_id 须留存，实际 {tomb!r}"
    assert tomb.get("local_user_id") == "u1", f"身份键须留存，实际 {tomb!r}"
    assert tomb.get("cloud_username") == "alice", tomb
    # 无 token 时 reset 幂等 false
    r = client.post("/api/client-exec/reset")
    assert r.json()["reset"] is False, "已无 token 时 reset 幂等 false"
    # 重配对复用墓碑 device_id
    client.post("/api/client-exec/pair", json={"pairing_code": "GOOD-CODE-1", "cloud_url": fake_cloud})
    assert getattr(_FakeCloudHandler, "last_body", {}).get("device_id") == "dev-KEEP-777"


def test_status_exposes_build_identity(store_env, fake_cloud, monkeypatch):
    """P1#4：status 暴露构建世代（build_id + impl_marker）——现场一眼判代。"""
    client = _client()
    body = client.get("/api/client-exec/status").json()
    assert body.get("impl_marker") == "W20-device-identity", body
    assert body.get("build_id"), body
    monkeypatch.setenv("WEAVE_BUILD_ID", "20261013-testsha")
    body = client.get("/api/client-exec/status").json()
    assert body.get("build_id") == "20261013-testsha", body


def test_pair_degrades_without_device_id_on_server_error(store_env, monkeypatch):
    """评审 r1-F1/r2-F1：携 device_id 撞上游 unique 约束（500）→ 去 id 重试成功 + 换代可见。"""
    import http.server
    import threading as _th

    class _FlakyHandler(_FakeCloudHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            type(self).last_body = dict(body)
            if body.get("device_id"):
                self._reply(500, {"detail": "unique constraint violated"})
                return
            self._reply(200, {
                "device_id": "dev-FRESH-NEW",
                "token": "fake-test-token-0000",
                "name": "cloud-name",
            })

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FlakyHandler)
    thread = _th.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        client = _client()
        r = client.post("/api/client-exec/pair", json={
            "pairing_code": "GOOD-CODE-1", "cloud_url": url,
            "device_id": "dev-OLD-CONT",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["device_id"] == "dev-FRESH-NEW", body
        assert body.get("device_id_changed") is True, body
    finally:
        srv.shutdown()
        thread.join(timeout=5)


def test_pair_identity_preserved_when_response_lacks_fields(store_env, fake_cloud):
    """评审 r1-F4：响应缺 user_id/username 时身份键回落墓碑（重配对不清身份）。"""
    client = _client()
    client.post("/api/client-exec/pair", json={"pairing_code": "GOOD-CODE-2", "cloud_url": fake_cloud})
    rec = json.loads((store_env / "app" / "device_exec.json").read_text(encoding="utf-8"))
    assert rec["cloud_user_id"] and rec["cloud_username"]
    uid, name = rec["cloud_user_id"], rec["cloud_username"]
    # 重配对（GOOD-CODE-1 响应无身份字段）→ 保留
    _FakeCloudHandler.last_body = {}
    r = client.post("/api/client-exec/pair", json={"pairing_code": "GOOD-CODE-1", "cloud_url": fake_cloud})
    assert r.status_code == 200, r.text
    rec = json.loads((store_env / "app" / "device_exec.json").read_text(encoding="utf-8"))
    assert rec["cloud_user_id"] == uid and rec["cloud_username"] == name, rec


def test_pair_device_id_new_escape_hatch(store_env, fake_cloud):
    """评审 r2-F3：device_id="new" 强制新代（异属主/格式冲突解死局）。"""
    client = _client()
    client.post("/api/client-exec/pair", json={
        "pairing_code": "GOOD-CODE-1", "cloud_url": fake_cloud, "device_id": "dev-STUCK-1",
    })
    _FakeCloudHandler.last_body = {}
    r = client.post("/api/client-exec/pair", json={
        "pairing_code": "GOOD-CODE-1", "cloud_url": fake_cloud, "device_id": "new",
    })
    assert r.status_code == 200, r.text
    assert not getattr(_FakeCloudHandler, "last_body", {}).get("device_id"), (
        f"逃生口不得携带旧 id，实际 {getattr(_FakeCloudHandler, 'last_body', {})!r}")
    assert r.json()["device_id"] == "dev-0001", r.json()


def test_pair_not_reuse_device_id_across_clouds(store_env, fake_cloud):
    """评审 r1-F5：换云不携带旧云 device_id。"""
    client = _client()
    client.post("/api/client-exec/pair", json={"pairing_code": "GOOD-CODE-1", "cloud_url": fake_cloud})
    rec = json.loads((store_env / "app" / "device_exec.json").read_text(encoding="utf-8"))
    assert rec.get("device_id")
    _FakeCloudHandler.last_body = {}
    r = client.post("/api/client-exec/pair", json={
        "pairing_code": "GOOD-CODE-1", "cloud_url": "http://127.0.0.1:9",  # 不同云
    })
    # 不同云 + 上游不可达 → 502；但 body 不得带旧云 device_id（先于网络失败可观测）
    assert r.status_code == 502, r.text
    assert not getattr(_FakeCloudHandler, "last_body", {}).get("device_id"), (
        "换云不得复用旧云 device_id")


def test_status_exposes_cloud_identity_fields(store_env, fake_cloud):
    """评审 r2-F6：P2#6 身份三键有腿。"""
    client = _client()
    client.post("/api/client-exec/pair", json={"pairing_code": "GOOD-CODE-2", "cloud_url": fake_cloud})
    body = client.get("/api/client-exec/status").json()
    assert body.get("cloud_username") == "alice", body
    assert body.get("cloud_user_id") == "cafef00d-0000-4000-8000-000000000000", body
    assert body.get("workspace_ctx_expected") == "alice-cafef00d", body
    # 墓碑态：device_id 面（凭据面）为空，身份面（墓碑）仍在（评审 r2-F2 契约腿）
    client.post("/api/client-exec/reset")
    body = client.get("/api/client-exec/status").json()
    assert body.get("paired") is False and body.get("device_id") == "", body
    assert body.get("cloud_username") == "alice", "墓碑身份面须仍可见"
