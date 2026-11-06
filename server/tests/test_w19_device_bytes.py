"""W19 P1 — 设备字节通道 / vision·asr 混合注入 / 远端执行审计（RED 先行）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_w19_device_bytes.py -v
"""
import asyncio
import base64
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.services.device_session_service import (
    DeviceCallError,
    DeviceSessionManager,
    device_session_manager,
)


class FakeWS:
    def __init__(self, responder=None):
        self.sent = []
        self._responder = responder

    async def send_json(self, payload):
        self.sent.append(payload)
        if self._responder:
            await self._responder(self, payload)

    async def close(self, code=1000):
        pass


class FakeConversation:
    def __init__(self, target):
        self.execution_target = target


class FakeUser:
    id = "uid-012345678ab"
    username = "tester"


def _resp(mgr, device_id, payload):
    async def _r(ws, req):
        mgr.handle_message(device_id, {"id": req["id"], "result": payload})
    return _r


class TestFetchFileBytes:
    def test_single_packet(self):
        mgr = DeviceSessionManager()
        data = b"hello-bytes"
        ws = FakeWS(_resp(mgr, "dev1", {"ok": True, "data_b64": base64.b64encode(data).decode(), "size": len(data)}))
        mgr.register("dev1", "u1", ws)

        out = asyncio.run(mgr.fetch_file_bytes("dev1", "fs/download", {"path": "a.png"}))
        assert out == data
        assert ws.sent[0]["method"] == "fs/download"

    def test_chunked_assembly_with_sha(self):
        mgr = DeviceSessionManager()
        data = b"0123456789" * 3
        mid = len(data) // 2
        c1, c2 = data[:mid], data[mid:]

        async def responder(ws, req):
            tid = "t1"
            mgr.handle_message("dev1", {"id": req["id"], "result": {"ok": True, "transfer_id": tid, "size": len(data)}})
            mgr.handle_message("dev1", {"method": "chunk", "params": {
                "transfer_id": tid, "seq": 0,
                "data_b64": base64.b64encode(c1).decode(),
                "sha256": hashlib.sha256(c1).hexdigest(),
            }})
            mgr.handle_message("dev1", {"method": "chunk", "params": {
                "transfer_id": tid, "seq": 1,
                "data_b64": base64.b64encode(c2).decode(),
                "sha256": hashlib.sha256(c2).hexdigest(),
                "last": True,
            }})

        ws = FakeWS(responder)
        sess = mgr.register("dev1", "u1", ws)
        out = asyncio.run(mgr.fetch_file_bytes("dev1", "fs/download", {"path": "b.bin"}))
        assert out == data

    def test_chunk_sha_mismatch_rejected(self):
        mgr = DeviceSessionManager()

        async def responder(ws, req):
            mgr.handle_message("dev1", {"id": req["id"], "result": {"ok": True, "transfer_id": "t2", "size": 3}})
            mgr.handle_message("dev1", {"method": "chunk", "params": {
                "transfer_id": "t2", "seq": 0, "data_b64": base64.b64encode(b"abc").decode(),
                "sha256": "0" * 64, "last": True,
            }})

        ws = FakeWS(responder)
        mgr.register("dev1", "u1", ws)
        with pytest.raises(DeviceCallError):
            asyncio.run(mgr.fetch_file_bytes("dev1", "fs/download", {"path": "c.bin"}))

    def test_oversize_rejected_before_decode(self):
        mgr = DeviceSessionManager()
        big = "A" * 2000
        ws = FakeWS(_resp(mgr, "dev1", {"ok": True, "data_b64": big, "size": 99999999}))
        mgr.register("dev1", "u1", ws)
        with pytest.raises(DeviceCallError):
            asyncio.run(mgr.fetch_file_bytes("dev1", "fs/download", {"path": "d.bin"}, max_bytes=100))

    def test_error_result_raises(self):
        mgr = DeviceSessionManager()
        ws = FakeWS(_resp(mgr, "dev1", {"ok": False, "error": "nope"}))
        mgr.register("dev1", "u1", ws)
        with pytest.raises(DeviceCallError):
            asyncio.run(mgr.fetch_file_bytes("dev1", "fs/download", {"path": "e.bin"}))


class TestHybridByteInject:
    def test_vision_fetches_and_injects(self, monkeypatch):
        from app.services import remote_dispatch_router as rdr

        calls = {}

        async def fake_fetch(device_id, method, params, max_bytes=None, expected_user_id=None):
            calls["device_id"] = device_id
            calls["params"] = params
            return b"IMG"

        monkeypatch.setattr(rdr, "fetch_file_bytes", fake_fetch, raising=False)

        kwargs = {"conversation": FakeConversation("device:abc"), "user": FakeUser()}
        out = asyncio.run(
            rdr.try_remote_dispatch("vision_interpret", {"image_path": "a.png", "question": "q"}, kwargs)
        )
        assert out is None  # 继续本地执行
        assert kwargs["file_bytes_override"] == b"IMG"
        assert calls["params"]["path"] == "a.png"

    def test_asr_fetches_and_injects(self, monkeypatch):
        from app.services import remote_dispatch_router as rdr

        async def fake_fetch(device_id, method, params, max_bytes=None, expected_user_id=None):
            return b"AUD"

        monkeypatch.setattr(rdr, "fetch_file_bytes", fake_fetch, raising=False)
        kwargs = {"conversation": FakeConversation("device:abc"), "user": FakeUser()}
        out = asyncio.run(
            rdr.try_remote_dispatch("asr_transcribe", {"audio_path": "a.wav"}, kwargs)
        )
        assert out is None
        assert kwargs["file_bytes_override"] == b"AUD"

    def test_hybrid_offline_fail_fast(self, monkeypatch):
        from app.services import remote_dispatch_router as rdr

        async def fake_fetch(device_id, method, params, max_bytes=None, expected_user_id=None):
            raise rdr.DeviceOfflineError("offline")

        monkeypatch.setattr(rdr, "fetch_file_bytes", fake_fetch, raising=False)
        kwargs = {"conversation": FakeConversation("device:abc"), "user": FakeUser()}
        out = asyncio.run(
            rdr.try_remote_dispatch("vision_interpret", {"image_path": "a.png"}, kwargs)
        )
        assert out is not None
        assert "离线" in out
        assert "file_bytes_override" not in kwargs


class TestHandlerOverride:
    def test_vision_uses_override_without_disk(self, monkeypatch):
        """A4.9 I-4 判别力腿：override 存在时不得走磁盘解析（盘上无此文件）。
        桩真实符号 _resolve_vision（此前误桩不存在的 _get_vlm_service 致假绿）。"""
        from app.tools import vision_interpret as vi

        async def fake_resolve(assistant):
            return (None, "no_vision")

        monkeypatch.setattr(vi, "_resolve_vision", fake_resolve)

        args = {"image_path": "not-on-disk.png", "question": "这是什么"}
        out_override = asyncio.run(
            vi.vision_interpret(args, workspace_path="/tmp/w", file_bytes_override=b"\x89PNG\r\n")
        )
        # 磁盘解析被跳过：不应报「图片不存在」
        assert "图片不存在" not in out_override, out_override
        # 走到了网关解析段（此处以 no_vision 受控早退）
        assert "视觉" in out_override

        out_no_override = asyncio.run(
            vi.vision_interpret(dict(args), workspace_path="/tmp/w")
        )
        # 对照：无 override 时磁盘解析失败（判别力——旧空断言无法区分两者）
        assert "图片不存在" in out_no_override, out_no_override


class TestExecAudit:
    def test_record_and_query(self):
        import asyncio as aio

        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import Session

        from app.db.database import Base, ClientExecAudit, User

        engine = create_engine("sqlite:///:memory:")

        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _record):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(engine, tables=[User.__table__, ClientExecAudit.__table__])
        sess = Session(engine)
        sess.add(User(id="u1", username="u1", password_hash="x", is_active=True))
        sess.commit()

        class Bridge:
            def __init__(self, s):
                self._s = s

            async def execute(self, stmt):
                return self._s.execute(stmt)

            async def commit(self):
                self._s.commit()

            def add(self, obj):
                self._s.add(obj)

            async def flush(self):
                self._s.flush()

            async def refresh(self, obj):
                self._s.refresh(obj)

        from app.services.client_exec_audit_service import record_exec, recent_execs

        db = Bridge(sess)
        aio.run(record_exec(db, user_id="u1", device_id="d1", conversation_id="c1",
                            tool="terminal", ok=True, error=None, duration_ms=12))
        aio.run(record_exec(db, user_id="u1", device_id="d1", conversation_id="c1",
                            tool="git", ok=False, error="boom", duration_ms=30))
        rows = aio.run(recent_execs(db, user_id="u1", limit=10))
        assert len(rows) == 2
        assert rows[0].tool == "git"  # 倒序
        assert rows[1].ok is True
