"""W16 设备执行模式 — client_device_service 配对/token 单元测试（TDD RED 先行）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_client_device_service.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.services import client_device_service as cds


class TestPairingCode:
    def test_issue_and_consume(self):
        rec = cds.issue_pairing_code("user-1")
        assert rec["code"] and len(rec["code"]) >= 8
        assert cds.consume_pairing_code(rec["code"], "user-1") == "user-1"

    def test_single_use(self):
        rec = cds.issue_pairing_code("user-1")
        assert cds.consume_pairing_code(rec["code"], "user-1") == "user-1"
        assert cds.consume_pairing_code(rec["code"], "user-1") is None

    def test_wrong_user_rejected(self):
        rec = cds.issue_pairing_code("user-1")
        assert cds.consume_pairing_code(rec["code"], "user-2") is None

    def test_unknown_code_rejected(self):
        assert cds.consume_pairing_code("nope", "user-1") is None

    def test_expired_code_rejected(self):
        rec = cds.issue_pairing_code("user-1", ttl_seconds=-1)
        assert cds.consume_pairing_code(rec["code"], "user-1") is None


class TestToken:
    def test_generate_and_hash(self):
        token, token_hash = cds.generate_device_token()
        assert len(token) >= 32
        assert cds.hash_device_token(token) == token_hash
        assert cds.hash_device_token(token) != token

    def test_hash_stable(self):
        assert cds.hash_device_token("abc") == cds.hash_device_token("abc")


class TestNormalizeTarget:
    def test_none_passthrough(self):
        assert cds.normalize_execution_target(None) is None
        assert cds.normalize_execution_target("") is None

    def test_device_target_ok(self):
        assert cds.normalize_execution_target("device:deadbeef") == "device:deadbeef"

    def test_bad_prefix_rejected(self):
        with pytest.raises(ValueError):
            cds.normalize_execution_target("cloud:x")

    def test_empty_device_id_rejected(self):
        with pytest.raises(ValueError):
            cds.normalize_execution_target("device:")

    def test_injection_rejected(self):
        with pytest.raises(ValueError):
            cds.normalize_execution_target("device:abc;drop")


class TestBruteForceGate:
    def test_unknown_code_guesses_hit_fail_window(self):
        # A4.9 r2 I-1：未知码猜解必须累计失败窗（consume 侧）
        from app.services import client_device_service as mod

        mod._fail_log.clear()
        for _ in range(5):
            assert mod.consume_pairing_code("99999999", "user-x") is None
        assert len(mod._fail_log) == 5

    def test_register_device_unknown_code_counts_fail(self):
        import asyncio as aio

        from app.services import client_device_service as mod

        mod._fail_log.clear()

        class _FakeDB:
            def add(self, obj):
                raise AssertionError("should not reach db")

        with pytest.raises(PermissionError):
            aio.run(mod.register_device(_FakeDB(), code="12345678"))
        assert len(mod._fail_log) >= 1


_ENGINES = []


def _sqlite_bridge():
    """同 id 重注册腿专用：内存 SQLite + async 鸭子壳（沿 test_device_exec_api 模式）。"""
    import asyncio  # noqa: F401
    from sqlalchemy import create_engine, event, select  # noqa: F401
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.database import Base, ClientExecutorDevice, ClientExecutorDeviceTombstone, User

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    tables, stack, seen = [], [ClientExecutorDevice.__table__, ClientExecutorDeviceTombstone.__table__], set()
    while stack:
        t = stack.pop()
        if t.name in seen:
            continue
        seen.add(t.name)
        tables.append(t)
        for col in t.columns:
            for fk in col.foreign_keys:
                stack.append(fk.column.table)
    Base.metadata.create_all(engine, tables=tables)
    sess = Session(engine)
    sess.add(User(id="u-aaaaaaaaaaaa", username="alice", password_hash="x", is_active=True))
    sess.add(User(id="u-bbbbbbbbbbbb", username="bob", password_hash="x", is_active=True))
    sess.commit()

    class _Bridge:
        def __init__(self, s):
            self._s = s

        async def execute(self, stmt):
            return self._s.execute(stmt)

        async def commit(self):
            self._s.commit()

        async def rollback(self):
            self._s.rollback()

        def add(self, obj):
            self._s.add(obj)

        async def delete(self, obj):
            self._s.delete(obj)

        async def flush(self):
            self._s.flush()

        async def refresh(self, obj):
            self._s.refresh(obj)

    _ENGINES.append(engine)
    return sess, _Bridge(sess), engine


@pytest.fixture(autouse=True)
def _dispose_engines():
    yield
    while _ENGINES:
        try:
            _ENGINES.pop().dispose()
        except Exception:
            pass


class TestDeviceIdContinuity:
    """遗留清偿 #1：同属主同 device_id 重注册=同行换 token 保身份（conv 目标不孤儿化）。"""

    def test_reregister_same_device_rotates_token_keeps_row(self):
        import asyncio as aio

        sess, db, _engine = _sqlite_bridge()
        code1 = cds.issue_pairing_code("u-aaaaaaaaaaaa")["code"]
        row1, tok1 = aio.run(cds.register_device(db, code=code1, device_id="cafebabe00112233", name="dev"))
        code2 = cds.issue_pairing_code("u-aaaaaaaaaaaa")["code"]
        row2, tok2 = aio.run(cds.register_device(db, code=code2, device_id="cafebabe00112233", name="dev2"))
        assert row2.id == row1.id, "同 id 重注册不得新建行"
        assert row2.device_id == "cafebabe00112233"
        assert tok2 != tok1, "必须换 token"
        assert aio.run(cds.verify_device_token(db, tok2)) is not None
        assert aio.run(cds.verify_device_token(db, tok1)) is None, "旧 token 必须失效"

    def test_reregister_foreign_device_rejected(self):
        import asyncio as aio

        sess, db, _engine = _sqlite_bridge()
        code1 = cds.issue_pairing_code("u-aaaaaaaaaaaa")["code"]
        aio.run(cds.register_device(db, code=code1, device_id="cafebabe00112233"))
        code2 = cds.issue_pairing_code("u-bbbbbbbbbbbb")["code"]
        with pytest.raises(PermissionError):
            aio.run(cds.register_device(db, code=code2, device_id="cafebabe00112233"))

    def test_reregister_after_revoke_keeps_device_id_continuity(self):
        # revoke=删行（用户裁定）后同 id 重配对=同 id 新插——历史会话目标不孤儿化
        import asyncio as aio

        sess, db, _engine = _sqlite_bridge()
        code1 = cds.issue_pairing_code("u-aaaaaaaaaaaa")["code"]
        row1, tok1 = aio.run(cds.register_device(db, code=code1, device_id="cafebabe00112233"))
        assert aio.run(cds.revoke_device(db, "u-aaaaaaaaaaaa", "cafebabe00112233")) is True
        code2 = cds.issue_pairing_code("u-aaaaaaaaaaaa")["code"]
        row2, tok2 = aio.run(cds.register_device(db, code=code2, device_id="cafebabe00112233"))
        assert row2.device_id == row1.device_id == "cafebabe00112233"
        assert aio.run(cds.verify_device_token(db, tok2)) is not None

    def test_reregister_legacy_revoked_row_revives_same_row(self):
        # 复活分支=迁移前存量 revoked 行的防御面（手工置 revoked 模拟存量）
        import asyncio as aio

        sess, db, _engine = _sqlite_bridge()
        code1 = cds.issue_pairing_code("u-aaaaaaaaaaaa")["code"]
        row1, tok1 = aio.run(cds.register_device(db, code=code1, device_id="cafebabe00112233"))
        row1.revoked = True
        sess.commit()
        code2 = cds.issue_pairing_code("u-aaaaaaaaaaaa")["code"]
        row2, tok2 = aio.run(cds.register_device(db, code=code2, device_id="cafebabe00112233"))
        assert row2.id == row1.id, "存量 revoked 行应复活为同一行"
        sess.refresh(row2)
        assert row2.revoked is False
        assert aio.run(cds.verify_device_token(db, tok2)) is not None

    def test_register_owner_missing_rejected(self):
        # 前置身份校验（r4 I-b）：属主不存在=拒绝，不烧 token 不动行
        import asyncio as aio

        sess, db, _engine = _sqlite_bridge()
        code = cds.issue_pairing_code("u-ghostghostgho")["code"]
        with pytest.raises(PermissionError):
            aio.run(cds.register_device(db, code=code, device_id="cafebabe00112234"))

    def test_register_owner_empty_username_rejected(self):
        import asyncio as aio

        from app.db.database import User

        sess, db, _engine = _sqlite_bridge()
        sess.add(User(id="u-emptyname00002", username="", password_hash="x", is_active=True))
        sess.commit()
        code = cds.issue_pairing_code("u-emptyname00002")["code"]
        with pytest.raises(PermissionError):
            aio.run(cds.register_device(db, code=code, device_id="cafebabe00112235"))


class TestDeviceTombstone:
    """遗留清偿收口：释放的 device_id 进墓碑——仅原属主可认领（抢占 DoS 关死）。"""

    def test_revoke_writes_tombstone_and_owner_reclaims(self):
        import asyncio as aio

        from sqlalchemy import select
        from app.db.database import ClientExecutorDeviceTombstone

        sess, db, _engine = _sqlite_bridge()
        code1 = cds.issue_pairing_code("u-aaaaaaaaaaaa")["code"]
        row1, tok1 = aio.run(cds.register_device(db, code=code1, device_id="feedface00112233"))
        assert aio.run(cds.revoke_device(db, "u-aaaaaaaaaaaa", "feedface00112233")) is True
        tomb = sess.execute(
            select(ClientExecutorDeviceTombstone).where(
                ClientExecutorDeviceTombstone.device_id == "feedface00112233"
            )
        ).scalar_one_or_none()
        assert tomb is not None and tomb.user_id == "u-aaaaaaaaaaaa"
        # 原属主认领=清墓碑+同 id 新插
        code2 = cds.issue_pairing_code("u-aaaaaaaaaaaa")["code"]
        row2, tok2 = aio.run(cds.register_device(db, code=code2, device_id="feedface00112233"))
        assert row2.device_id == "feedface00112233"
        tomb2 = sess.execute(
            select(ClientExecutorDeviceTombstone).where(
                ClientExecutorDeviceTombstone.device_id == "feedface00112233"
            )
        ).scalar_one_or_none()
        assert tomb2 is None, "认领后墓碑必须清除"
        assert aio.run(cds.verify_device_token(db, tok2)) is not None

    def test_foreign_claim_of_freed_id_rejected(self):
        import asyncio as aio

        sess, db, _engine = _sqlite_bridge()
        code1 = cds.issue_pairing_code("u-aaaaaaaaaaaa")["code"]
        aio.run(cds.register_device(db, code=code1, device_id="feedface00112233"))
        aio.run(cds.revoke_device(db, "u-aaaaaaaaaaaa", "feedface00112233"))
        # 他户认领已释放 id（抢占 DoS/劫持纵深）——墓碑闸拒绝
        code2 = cds.issue_pairing_code("u-bbbbbbbbbbbb")["code"]
        with pytest.raises(PermissionError):
            aio.run(cds.register_device(db, code=code2, device_id="feedface00112233"))

    def test_integrity_race_maps_to_permission_error(self):
        # NEW-M3（r4）：并发同 id 两码竞态撞 UNIQUE→403 语义（非 500）
        import asyncio as aio

        class _RacingDB:
            def __init__(self, inner):
                self._i = inner

            async def execute(self, stmt):
                return await self._i.execute(stmt)

            async def commit(self):
                raise Exception("unique constraint failed: client_executor_devices.device_id")

            async def rollback(self):
                return await self._i.rollback()

            async def delete(self, obj):
                return await self._i.delete(obj)

            def add(self, obj):
                self._i.add(obj)

            async def refresh(self, obj):
                return await self._i.refresh(obj)

        sess, db, _engine = _sqlite_bridge()
        racing = _RacingDB(db)
        code = cds.issue_pairing_code("u-aaaaaaaaaaaa")["code"]
        with pytest.raises(PermissionError):
            aio.run(cds.register_device(racing, code=code, device_id="cafebabe00112236"))
