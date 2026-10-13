"""T1（PLAN_device_workspace_identity_impl）— SyncState.cloud_user_id + 绑定取数。

名门期望值=云端身份导出 `{slug(cloud_username)}-{cloud_user_id[:8]}`（Hub 行），
设备侧须在 sync 绑定（PUT /api/sync/account）时持久化云端用户 id——否则名门
期望值无从计算（workspace_mismatch 全拒事故的前置修复）。

Run:  .localstack/venv/bin/python -m pytest server/tests/test_sync_account_cloud_identity.py -q
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.migrations import STARTUP_MIGRATIONS  # noqa: E402


def test_migrations_register_cloud_user_id_column():
    names = [v for v, _ in STARTUP_MIGRATIONS]
    assert "sync_state_cloud_user_id" in names, "缺启动迁移 sync_state_cloud_user_id"
    stmt = dict(STARTUP_MIGRATIONS)["sync_state_cloud_user_id"]
    assert "cloud_user_id" in stmt and "ADD COLUMN IF NOT EXISTS" in stmt


def test_syncstate_model_has_cloud_user_id():
    from app.db.database import SyncState

    assert hasattr(SyncState, "cloud_user_id"), "SyncState 缺 cloud_user_id 列"


class _CloudHandler(BaseHTTPRequestHandler):
    """假云端：login → token；/api/sync/status → 200；/api/auth/me → 云端身份。"""

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length or 0)
        if self.path == "/api/auth/login":
            self._json(200, {"access_token": "tok-me-1", "token_type": "bearer"})
        else:
            self._json(404, {"detail": "no"})

    def do_GET(self):
        if self.path == "/api/sync/status":
            self._json(200, {"ok": True})
        elif self.path == "/api/auth/me":
            self._json(200, {"id": "cafef00d-0000-4000-8000-000000000000",
                             "username": "alice"})
        else:
            self._json(404, {"detail": "no"})


def test_put_sync_account_persists_cloud_user_id(monkeypatch):
    from app.api.sync import put_sync_account
    from app.core.config import get_config

    config = get_config()
    saved = config._config.get("sync")
    config._config["sync"] = {"enabled": True}
    try:
        _run_put(monkeypatch)
    finally:
        if saved is None:
            config._config.pop("sync", None)
        else:
            config._config["sync"] = saved


def _run_put(monkeypatch):
    from app.api.sync import put_sync_account

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        row = SimpleNamespace(user_id="u1", device_id="d1", server_url=None,
                              cloud_username=None, cloud_password=None,
                              sync_enabled=False, tls_verify=True,
                              push_cursor=0, pull_cursor=0,
                              last_error=None, last_sync_at=None)

        class _Sess:
            async def get(self, model, key):
                return row

            async def commit(self):
                pass

            def add(self, obj):
                pass

        body = asyncio.run(put_sync_account(
            request=SimpleNamespace(json=lambda: asyncio.sleep(0, result={
                "server_url": url, "cloud_username": "alice",
                "cloud_password": "pw-x", "tls_verify": True,
            })),
            db=_Sess(), current_user=SimpleNamespace(id="u1"),
        ))
        assert body["bound"] is True
        assert getattr(row, "cloud_user_id", None) == "cafef00d-0000-4000-8000-000000000000", (
            f"绑定必须持久化云端用户 id（名门期望值来源），实际 {getattr(row, 'cloud_user_id', None)!r}")
    finally:
        srv.shutdown()
        thread.join(timeout=5)


def test_me_failure_preserves_existing_cloud_user_id(monkeypatch):
    """r1-F3/r2-F4：/api/auth/me 失败不得洗掉已验证的 cloud_user_id（换绑语义）。"""
    class _CloudHandler2(_CloudHandler):
        def do_GET(self):
            if self.path == "/api/auth/me":
                self._json(500, {"detail": "me down"})
            else:
                super().do_GET()

    from app.api.sync import put_sync_account
    from app.core.config import get_config

    config = get_config()
    saved = config._config.get("sync")
    config._config["sync"] = {"enabled": True}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHandler2)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        row = SimpleNamespace(user_id="u1", device_id="d1", server_url=url,
                              cloud_username="alice", cloud_password="old",
                              cloud_user_id="cafef00d-KEEPME", sync_enabled=True,
                              tls_verify=True, push_cursor=0, pull_cursor=0,
                              last_error=None, last_sync_at=None)

        class _Sess:
            async def get(self, model, key):
                return row

            async def commit(self):
                pass

            def add(self, obj):
                pass

        body = asyncio.run(put_sync_account(
            request=SimpleNamespace(json=lambda: asyncio.sleep(0, result={
                "server_url": url, "cloud_username": "alice",
                "cloud_password": "pw-new", "tls_verify": True,
            })),
            db=_Sess(), current_user=SimpleNamespace(id="u1"),
        ))
        assert body["bound"] is True
        assert row.cloud_user_id == "cafef00d-KEEPME", (
            f"me 失败必须保留旧 cloud_user_id，实际 {row.cloud_user_id!r}")
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        if saved is None:
            config._config.pop("sync", None)
        else:
            config._config["sync"] = saved


def test_sync_login_backfills_cloud_user_id(monkeypatch):
    """r2-F2 收口：sync 周期登录后惰性回填 SyncState.cloud_user_id（存量绑定自愈）。"""
    from types import SimpleNamespace as NS

    from app.services.sync_client import SyncClient

    row = NS(user_id="u1", cloud_user_id="")
    fetched = {}

    class _Client:
        async def post(self, path, json=None):
            fetched["login"] = True

            class _R:
                status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    return {"access_token": "tok-1"}
            return _R()

        async def get(self, path, headers=None):
            fetched["me"] = path

            class _R:
                status_code = 200

                def json(self):
                    return {"id": "cafef00d-0000-4000-8000-000000000000",
                            "username": "alice"}
            return _R()

    class _DB:
        async def get(self, model, key):
            return row

        async def commit(self):
            fetched["committed"] = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    import app.db.database as dbmod
    monkeypatch.setattr(dbmod, "AsyncSessionLocal", lambda: _DB())

    state = NS(user_id="u1", server_url="http://x", cloud_username="alice",
               cloud_password="pw", device_id="d1", cloud_user_id="")
    c = SyncClient()
    c._tokens[c._tkey(state)] = "tok-1"  # 已登录态（_login 挂接面由 ensure_future 驱动）
    asyncio.run(c._backfill_cloud_identity(state, _Client()))
    assert fetched.get("me") == "/api/auth/me", fetched
    assert row.cloud_user_id == "cafef00d-0000-4000-8000-000000000000", row.cloud_user_id
    assert fetched.get("committed") is True


def test_rebind_other_account_and_me_failure_clears_stale_uid(monkeypatch):
    """复评 NEW-1：换绑不同账号 + me 失败 → 旧 cloud_user_id 必须清空（防混合身份不可自愈）。"""
    class _CloudHandler2(_CloudHandler):
        def do_GET(self):
            if self.path == "/api/auth/me":
                self._json(500, {"detail": "me down"})
            else:
                super().do_GET()

    from app.api.sync import put_sync_account
    from app.core.config import get_config

    config = get_config()
    saved = config._config.get("sync")
    config._config["sync"] = {"enabled": True}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHandler2)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        row = SimpleNamespace(user_id="u1", device_id="d1", server_url="http://old-host",
                              cloud_username="old-account", cloud_password="old",
                              cloud_user_id="olduid99-STALE", sync_enabled=True,
                              tls_verify=True, push_cursor=0, pull_cursor=0,
                              last_error=None, last_sync_at=None)

        class _Sess:
            async def get(self, model, key):
                return row

            async def execute(self, stmt):
                class _R:
                    def scalar(self):
                        return 0
                return _R()

            async def commit(self):
                pass

            def add(self, obj):
                pass

        asyncio.run(put_sync_account(
            request=SimpleNamespace(json=lambda: asyncio.sleep(0, result={
                "server_url": url, "cloud_username": "new-account",
                "cloud_password": "pw-new", "tls_verify": True,
            })),
            db=_Sess(), current_user=SimpleNamespace(id="u1"),
        ))
        assert row.cloud_username == "new-account"
        assert not row.cloud_user_id, (
            f"换绑后旧 uid 必须清空（NEW-1），实际 {row.cloud_user_id!r}")
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        if saved is None:
            config._config.pop("sync", None)
        else:
            config._config["sync"] = saved
