"""W16 设备执行模式 — 配对/属主/不可变语义（A4.7 文档驱动测试，PLAN §6.2/§8）。

覆盖：配对码换 token · 坏码 403 · revoked 拒验 · IDOR 属主校验 ·
execution_target 创建即定（首设 adopt / 变更拒绝 / 坏格式 422 / 未撤销设备 400）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_device_exec_api.py -v
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.db.database import Base, ClientExecutorDevice, ClientExecutorDeviceTombstone, Conversation, Message, User
from app.services import client_device_service as cds


class _Bridge:
    """asyncio 鸭子壳包真实 sync Session（SQLite 内存）——沿 test_edit_resend_trim 模式。"""

    def __init__(self, sync_sess):
        self._s = sync_sess

    async def execute(self, stmt):
        return self._s.execute(stmt)

    async def commit(self):
        self._s.commit()

    async def rollback(self):
        self._s.rollback()

    async def delete(self, obj):
        self._s.delete(obj)

    def add(self, obj):
        self._s.add(obj)

    async def flush(self):
        self._s.flush()

    async def refresh(self, obj):
        self._s.refresh(obj)


_ENGINES = []


def _fk_closure_tables(*roots):
    """FK 传递闭包一次建全（沿 test_edit_resend_trim 模式，避开 JSONB 表）。
    遗留清偿 #5：_fresh/_fresh_http 共用（评审 A-m5 去重）。"""
    tables, stack, seen = [], list(roots), set()
    while stack:
        t = stack.pop()
        if t.name in seen:
            continue
        seen.add(t.name)
        tables.append(t)
        for col in t.columns:
            for fk in col.foreign_keys:
                stack.append(fk.column.table)
    return tables


@pytest.fixture(autouse=True)
def _dispose_engines():
    yield
    while _ENGINES:
        try:
            _ENGINES.pop().dispose()
        except Exception:
            pass


def _fresh():
    engine = create_engine("sqlite:///:memory:")
    _ENGINES.append(engine)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(
        engine,
        tables=_fk_closure_tables(
            ClientExecutorDevice.__table__, ClientExecutorDeviceTombstone.__table__,
            Conversation.__table__, Message.__table__,
        ),
    )
    sess = Session(engine)
    user = User(id="u-12345678aaaa", username="tester", password_hash="x", is_active=True)
    sess.add(user)
    sess.commit()
    return sess, _Bridge(sess), user


def _register(db, user_id, code=None):
    if code is None:
        code = cds.issue_pairing_code(user_id)["code"]
    return asyncio.run(cds.register_device(db, code=code, name="dev", platform="mac"))


class TestPairingFlow:
    def test_pair_and_verify(self):
        sess, db, user = _fresh()
        row, token = _register(db, user.id)
        assert row.user_id == user.id
        got = asyncio.run(cds.verify_device_token(db, token))
        assert got is not None
        assert got.device_id == row.device_id

    def test_bad_code_rejected(self):
        sess, db, user = _fresh()
        with pytest.raises(PermissionError):
            asyncio.run(cds.register_device(db, code="00000000"))

    def test_revoked_token_rejected(self):
        sess, db, user = _fresh()
        row, token = _register(db, user.id)
        row.revoked = True
        sess.commit()
        assert asyncio.run(cds.verify_device_token(db, token)) is None

    def test_revoke_scoped_to_owner(self):
        # 用户指令变更（遗留清偿 #7）：revoke=删设备行（解绑后不留列表）；
        # 审计 client_exec_audit 留痕不在此表。
        sess, db, user = _fresh()
        row, token = _register(db, user.id)
        ok = asyncio.run(cds.revoke_device(db, "someone-else", row.device_id))
        assert ok is False
        ok = asyncio.run(cds.revoke_device(db, user.id, row.device_id))
        assert ok is True
        gone = sess.execute(
            __import__("sqlalchemy").select(ClientExecutorDevice).where(
                ClientExecutorDevice.device_id == row.device_id
            )
        ).scalar_one_or_none()
        assert gone is None, "revoke 必须删除设备行（解绑设备不留列表）"
        assert asyncio.run(cds.verify_device_token(db, token)) is None


class TestAssertDeviceUsable:
    def test_owned_ok(self):
        sess, db, user = _fresh()
        row, _token = _register(db, user.id)
        got = asyncio.run(cds.assert_device_usable(db, user.id, f"device:{row.device_id}"))
        assert got.device_id == row.device_id

    def test_idor_rejected(self):
        sess, db, user = _fresh()
        row, _token = _register(db, user.id)
        with pytest.raises(LookupError):
            asyncio.run(cds.assert_device_usable(db, "other-user", f"device:{row.device_id}"))

    def test_revoked_rejected(self):
        sess, db, user = _fresh()
        row, _token = _register(db, user.id)
        row.revoked = True
        sess.commit()
        with pytest.raises(LookupError):
            asyncio.run(cds.assert_device_usable(db, user.id, f"device:{row.device_id}"))

    def test_list_devices_excludes_revoked(self):
        # 遗留清偿 #7：存量 revoked 行（迁移前残留）不得出现在列表
        sess, db, user = _fresh()
        row, _token = _register(db, user.id)
        row.revoked = True
        sess.commit()
        rows = asyncio.run(cds.list_devices(db, user.id))
        assert all(r.device_id != row.device_id for r in rows)


class TestExecutionTargetImmutability:
    def test_adopt_first_set(self):
        sess, db, user = _fresh()
        row, _token = _register(db, user.id)
        conv = Conversation(user_id=user.id, title="t")
        sess.add(conv)
        sess.commit()

        from app.api.chat import _resolve_execution_target_for_conversation

        asyncio.run(
            _resolve_execution_target_for_conversation(
                db, user, conv, f"device:{row.device_id}"
            )
        )
        assert conv.execution_target == f"device:{row.device_id}"

    def test_change_rejected(self):
        sess, db, user = _fresh()
        row, _token = _register(db, user.id)
        conv = Conversation(user_id=user.id, title="t", execution_target="device:fixed1")
        sess.add(conv)
        sess.commit()

        from app.api.chat import _resolve_execution_target_for_conversation
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            asyncio.run(
                _resolve_execution_target_for_conversation(
                    db, user, conv, f"device:{row.device_id}"
                )
            )
        assert ei.value.status_code == 400

    def test_bad_format_422(self):
        sess, db, user = _fresh()
        conv = Conversation(user_id=user.id, title="t")
        sess.add(conv)
        sess.commit()

        from app.api.chat import _resolve_execution_target_for_conversation
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            asyncio.run(
                _resolve_execution_target_for_conversation(db, user, conv, "cloud:x")
            )
        assert ei.value.status_code == 422

    def test_missing_device_400(self):
        sess, db, user = _fresh()
        conv = Conversation(user_id=user.id, title="t")
        sess.add(conv)
        sess.commit()

        from app.api.chat import _resolve_execution_target_for_conversation
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            asyncio.run(
                _resolve_execution_target_for_conversation(
                    db, user, conv, "device:ghost-device"
                )
            )
        assert ei.value.status_code == 400

    def test_same_target_noop(self):
        sess, db, user = _fresh()
        conv = Conversation(user_id=user.id, title="t", execution_target="device:fixed1")
        sess.add(conv)
        sess.commit()

        from app.api.chat import _resolve_execution_target_for_conversation

        asyncio.run(
            _resolve_execution_target_for_conversation(db, user, conv, "device:fixed1")
        )
        assert conv.execution_target == "device:fixed1"


class TestRelPathHardening:
    def _check(self, path):
        from app.api.client_devices import _check_rel_path
        return _check_rel_path(path)

    def test_plain_rel_ok(self):
        assert self._check("a.txt") == "a.txt"
        assert self._check("dir/sub/a.txt") == "dir/sub/a.txt"

    @pytest.mark.parametrize("bad", [
        "", "/etc/passwd", "//x", "../x", "a/../b", "a/./b", "a//b",
        "..\\windows\\x", "C:/Windows/x", "\\\\server\\share", "~/.ssh/id",
        "a\x00b", "a\nb", "x" * 2000,
    ])
    def test_traversal_and_absolute_rejected(self, bad):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            self._check(bad)
        assert ei.value.status_code == 400

    def test_content_disposition_non_ascii(self):
        # A4.9 I-4：中文文件名不再 500/头注入
        from app.api.client_devices import _content_disposition
        cd = _content_disposition("dir/中文文档.txt")
        assert cd.startswith("attachment;")
        assert "\r" not in cd and "\n" not in cd
        assert "filename*=UTF-8''" in cd

    def test_content_disposition_strips_quotes(self):
        from app.api.client_devices import _content_disposition
        cd = _content_disposition('a"b.txt')
        assert 'a"b' not in cd


class TestTrailingSegmentHardening:
    def test_trailing_dot_space_segments_rejected(self):
        # A4.9 r2：Win32 尾点/尾空格剥离陷阱
        from fastapi import HTTPException

        from app.api.client_devices import _check_rel_path
        for bad in (".. /x", "a/.. ", "x. /y", "a/b "):
            with pytest.raises(HTTPException):
                _check_rel_path(bad)


def _fresh_http():
    """HTTP 腿专用：check_same_thread=False —— TestClient 在 worker 线程跑 handler，
    SQLite 连接跨线程复用（仅本波新增测试使用，不改既有 _fresh）。"""
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _ENGINES.append(engine)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(
        engine,
        tables=_fk_closure_tables(
            ClientExecutorDevice.__table__, ClientExecutorDeviceTombstone.__table__,
            Conversation.__table__, Message.__table__,
        ),
    )
    sess = Session(engine)
    user = User(id="u-12345678aaaa", username="tester", password_hash="x", is_active=True)
    sess.add(user)
    sess.commit()
    return sess, _Bridge(sess), user


def _pair_client(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import client_devices
    from app.core.deps import get_db

    app = FastAPI()
    app.include_router(client_devices.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


class TestPairResponseCloudIdentity:
    """handoff ①：pair 响应交换属主云端身份（user_id/username）——下游
    client_exec.pair 直接读 data.get("user_id")/data.get("username") 落盘。
    TDD RED 先行（2026-10-12 handoff 波）。"""

    def test_pair_response_carries_cloud_identity(self):
        sess, db, user = _fresh_http()
        code = cds.issue_pairing_code(user.id)["code"]
        r = _pair_client(db).post(
            "/api/devices/pair",
            json={"code": code, "name": "dev", "platform": "mac"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user_id"] == user.id
        assert body["username"] == "tester"

    def test_pair_response_identity_fields_are_strings(self):
        # 下游 str(data.get("user_id") or "") 落盘契约：user_id 必须是非空字符串
        sess, db, user = _fresh_http()
        code = cds.issue_pairing_code(user.id)["code"]
        r = _pair_client(db).post(
            "/api/devices/pair",
            json={"code": code, "name": "dev", "platform": "mac"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body["user_id"], str) and body["user_id"]
        assert isinstance(body.get("username"), str)

    def test_pair_owner_missing_rejected(self, monkeypatch):
        # 评审 A-I3：owner 行缺失（配对码签发后用户被删竞态）→ 403 拒绝，
        # 不发毒 username=null（下游名门期望值会算出永不匹配的 user-<id8>）。
        sess, db, user = _fresh_http()

        class _GhostRow:
            device_id = "deadbeef00112233445566778899aabb"
            user_id = "ghost-user-00000000"
            name = "dev"

        async def fake_register(db, *, code, name=None, platform=None, device_id=None):
            return _GhostRow(), "tok-1"

        monkeypatch.setattr(cds, "register_device", fake_register)
        r = _pair_client(db).post(
            "/api/devices/pair", json={"code": "x", "name": "dev", "platform": "mac"}
        )
        assert r.status_code == 403, r.text
        assert "属主用户不存在" in r.text
        assert "user_id" not in (r.json() or {})

    def test_pair_owner_identity_abnormal_message(self, monkeypatch):
        # 遗留清偿 #4：owner 在但 username 空（病态）→ 文案可区分（非「用户不存在」）
        sess, db, user = _fresh_http()
        sess.add(User(id="u-emptyname00001", username="", password_hash="x", is_active=True))
        sess.commit()

        class _Row:
            device_id = "deadbeef00112233445566778899aacc"
            user_id = "u-emptyname00001"
            name = "dev"

        async def fake_register(db, *, code, name=None, platform=None, device_id=None):
            return _Row(), "tok-2"

        monkeypatch.setattr(cds, "register_device", fake_register)
        r = _pair_client(db).post(
            "/api/devices/pair", json={"code": "x", "name": "dev", "platform": "mac"}
        )
        assert r.status_code == 403, r.text
        assert "属主身份异常" in r.text


class TestDeviceRenameEndpoint:
    """handoff ③（2026-10-15，weave-thinker-client W25）：设备自改显示名端点。

    测试用例从 backend/API.md `POST /api/devices/{device_id}/rename` 契约写：
    200 锚 renamed:true · 401（无/无效/已撤销 token）· 403（device_id 不匹配）·
    400（缺字段/null/空白/超长>255 拒而不截断）· 404（exec 关闭）· token/device_id 不动。
    TDD RED 先行。"""

    _LONG = "名" * 256

    def _rename(self, db, device_id, token, body):
        client = _pair_client(db)
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return client.post(f"/api/devices/{device_id}/rename", json=body, headers=headers)

    def test_rename_happy_path_anchor(self):
        sess, db, user = _fresh_http()
        row, token = _register(db, user.id)
        r = self._rename(db, row.device_id, token, {"name": "客厅主力机"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["renamed"] is True, "renamed:true 是客户端防伪回执锚"
        assert body["device_id"] == row.device_id
        assert body["name"] == "客厅主力机"
        sess.expire_all()
        got = sess.execute(
            select(ClientExecutorDevice).where(
                ClientExecutorDevice.device_id == row.device_id
            )
        ).scalar_one()
        assert got.name == "客厅主力机"

    def test_rename_without_token_401(self):
        sess, db, user = _fresh_http()
        row, _token = _register(db, user.id)
        r = self._rename(db, row.device_id, None, {"name": "x"})
        assert r.status_code == 401, r.text

    def test_rename_invalid_token_401(self):
        sess, db, user = _fresh_http()
        row, _token = _register(db, user.id)
        r = self._rename(db, row.device_id, "not-a-real-device-token", {"name": "x"})
        assert r.status_code == 401, r.text

    def test_rename_revoked_device_401(self):
        sess, db, user = _fresh_http()
        row, token = _register(db, user.id)
        row.revoked = True
        sess.commit()
        r = self._rename(db, row.device_id, token, {"name": "x"})
        assert r.status_code == 401, r.text

    def test_rename_device_id_mismatch_403(self):
        sess, db, user = _fresh_http()
        _row, token = _register(db, user.id)
        r = self._rename(db, "other-device-0000", token, {"name": "x"})
        assert r.status_code == 403, r.text

    def test_rename_missing_name_400_not_422(self):
        # 家族口径（PairRequest 同款）：缺字段不走 422——处理器内 400
        sess, db, user = _fresh_http()
        row, token = _register(db, user.id)
        r = self._rename(db, row.device_id, token, {})
        assert r.status_code == 400, r.text

    def test_rename_null_name_400(self):
        sess, db, user = _fresh_http()
        row, token = _register(db, user.id)
        r = self._rename(db, row.device_id, token, {"name": None})
        assert r.status_code == 400, r.text

    def test_rename_blank_name_400(self):
        sess, db, user = _fresh_http()
        row, token = _register(db, user.id)
        r = self._rename(db, row.device_id, token, {"name": "   "})
        assert r.status_code == 400, r.text

    def test_rename_over_255_rejected_not_truncated(self):
        sess, db, user = _fresh_http()
        row, token = _register(db, user.id)
        original_name = row.name
        r = self._rename(db, row.device_id, token, {"name": self._LONG})
        assert r.status_code == 400, r.text
        sess.expire_all()
        got = sess.execute(
            select(ClientExecutorDevice).where(
                ClientExecutorDevice.device_id == row.device_id
            )
        ).scalar_one()
        assert got.name == original_name, "超长必须拒绝而非静默截断（云端/本地显示名分歧红线）"

    def test_rename_exactly_255_ok(self):
        sess, db, user = _fresh_http()
        row, token = _register(db, user.id)
        r = self._rename(db, row.device_id, token, {"name": "名" * 255})
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "名" * 255

    def test_rename_keeps_token_and_device_id(self):
        # 语义红线：改名是纯显示名修改——token/device_id 不动
        sess, db, user = _fresh_http()
        row, token = _register(db, user.id)
        r = self._rename(db, row.device_id, token, {"name": "新名字"})
        assert r.status_code == 200, r.text
        got = asyncio.run(cds.verify_device_token(db, token))
        assert got is not None and got.device_id == row.device_id
        assert got.name == "新名字"

    def test_rename_user_jwt_as_device_token_401(self):
        # 权限交叉：用户 JWT 冒充设备 token → 401（hash 查无）
        from app.services.auth_service import create_access_token

        sess, db, user = _fresh_http()
        row, _token = _register(db, user.id)
        jwt = create_access_token(user.id, user.username)
        r = self._rename(db, row.device_id, jwt, {"name": "x"})
        assert r.status_code == 401, r.text

    def test_rename_other_users_existing_device_403(self):
        # 评审 R1-m3：403 须锁死「他人真实存在 id」路径（防未来改成存在性分支）
        sess, db, user = _fresh_http()
        _row, token = _register(db, user.id)
        sess.add(User(id="u-other000000001", username="other", password_hash="x", is_active=True))
        sess.commit()
        other_row, _ot = _register(db, "u-other000000001")
        r = self._rename(db, other_row.device_id, token, {"name": "侵权"})
        assert r.status_code == 403, r.text

    def test_rename_vs_revoke_race_lookup_error(self):
        # 评审 R1-I1/R2-m1：verify 通过后行被 revoke（删）→ 必须 LookupError 而非
        # InvalidRequestError/500（确定性模拟：verify 与 rename 之间删行提交）
        sess, db, user = _fresh_http()
        row, token = _register(db, user.id)
        device = asyncio.run(cds.verify_device_token(db, token))
        assert device is not None
        sess.delete(row)
        sess.commit()
        with pytest.raises(LookupError):
            asyncio.run(cds.rename_device(db, device, "迟到改名"))

    def test_rename_vs_revoke_race_two_sessions(self):
        # 复审 R2-New-Important：生产形态=verify 会话 ≠ revoke 会话（两个 get_db session）。
        # StaticPool 同连接双 Session：B 删行 commit 后 A rename_device 的真实异常面。
        from sqlalchemy.orm import Session as SyncSession

        sess, db, user = _fresh_http()
        row, token = _register(db, user.id)
        device = asyncio.run(cds.verify_device_token(db, token))
        assert device is not None
        other = SyncSession(sess.get_bind())
        try:
            victim = other.execute(
                select(ClientExecutorDevice).where(
                    ClientExecutorDevice.device_id == row.device_id
                )
            ).scalar_one()
            other.delete(victim)
            other.commit()
        finally:
            other.close()
        with pytest.raises(LookupError):
            asyncio.run(cds.rename_device(db, device, "迟到改名"))

    def test_rename_endpoint_deleted_row_404(self, monkeypatch):
        sess, db, user = _fresh_http()
        row, token = _register(db, user.id)

        async def fake_rename(db_, row_, name):
            raise LookupError("设备不存在或已解绑")

        monkeypatch.setattr(cds, "rename_device", fake_rename)
        r = self._rename(db, row.device_id, token, {"name": "x"})
        assert r.status_code == 404, r.text

    def test_rename_exec_disabled_404(self, monkeypatch):
        from types import SimpleNamespace

        from app.api import client_devices as cd_api

        sess, db, user = _fresh_http()
        row, token = _register(db, user.id)
        monkeypatch.setattr(
            cd_api, "get_config", lambda: SimpleNamespace(client_exec_enabled=False)
        )
        r = self._rename(db, row.device_id, token, {"name": "x"})
        assert r.status_code == 404, r.text
        assert r.json().get("detail") == "device execution disabled", \
            "404 须可区分于路由缺失的 Not Found（判别力双断言）"


class TestFsProxySessionFence:
    """r4 fs 面竞态：DB 行属主 ≠ 在线会话属主 → 409（HTTP 面同栅栏）。"""

    def test_require_owned_device_session_mismatch_409(self):
        sess, db, user = _fresh()
        row, _token = _register(db, user.id)
        from app.api.client_devices import _require_owned_device
        from app.services.device_session_service import device_session_manager
        from fastapi import HTTPException

        original = device_session_manager.device_user_id
        device_session_manager.device_user_id = lambda did: "attacker-uid-9999"
        try:
            with pytest.raises(HTTPException) as ei:
                asyncio.run(_require_owned_device(db, user.id, row.device_id))
            assert ei.value.status_code == 409
        finally:
            device_session_manager.device_user_id = original

    def test_require_owned_device_same_owner_ok(self):
        sess, db, user = _fresh()
        row, _token = _register(db, user.id)
        from app.api.client_devices import _require_owned_device

        got = asyncio.run(_require_owned_device(db, user.id, row.device_id))
        assert got.device_id == row.device_id
