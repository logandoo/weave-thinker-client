"""W16 设备执行模式 — DeviceSessionManager 单元测试（TDD RED 先行）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_device_session_manager.py -v
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.services.device_session_service import (
    DeviceCallError,
    DeviceCallTimeoutError,
    DeviceOfflineError,
    DeviceSessionManager,
)


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def send_text(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000):
        self.sent.append({"_closed": code})


def test_register_and_online():
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)
    assert mgr.is_online("dev1") is True
    assert mgr.get("dev1").user_id == "user1"
    mgr.unregister("dev1")
    assert mgr.is_online("dev1") is False


def test_rpc_roundtrip_resolves_future():
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        task = asyncio.create_task(
            mgr.rpc_call("dev1", "tools/call", {"tool": "terminal"}, timeout=5)
        )
        await asyncio.sleep(0)  # let request go out
        assert len(ws.sent) == 1
        req = ws.sent[0]
        assert req["method"] == "tools/call"
        call_id = req["id"]
        mgr.handle_message("dev1", {"id": call_id, "result": {"ok": True, "content": "hi"}})
        return await task

    result = asyncio.run(run())
    assert result == {"ok": True, "content": "hi"}


def test_rpc_error_response_raises():
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        task = asyncio.create_task(mgr.rpc_call("dev1", "tools/call", {}, timeout=5))
        await asyncio.sleep(0)
        call_id = ws.sent[0]["id"]
        mgr.handle_message("dev1", {"id": call_id, "error": {"code": -1, "message": "boom"}})
        with pytest.raises(DeviceCallError):
            await task

    asyncio.run(run())


def test_rpc_offline_raises():
    mgr = DeviceSessionManager()

    async def run():
        with pytest.raises(DeviceOfflineError):
            await mgr.rpc_call("missing", "tools/call", {}, timeout=1)

    asyncio.run(run())


def test_rpc_timeout_raises():
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        with pytest.raises(DeviceCallTimeoutError):
            await mgr.rpc_call("dev1", "tools/call", {}, timeout=0.05)

    asyncio.run(run())


def test_heartbeat_updates_last_seen():
    import datetime

    mgr = DeviceSessionManager()
    ws = FakeWS()
    sess = mgr.register("dev1", "user1", ws)
    before = sess.last_seen
    sess.last_seen = datetime.datetime(2000, 1, 1)
    mgr.handle_message("dev1", {"method": "heartbeat"})
    assert mgr.get("dev1").last_seen > datetime.datetime(2000, 1, 1)


def test_disconnect_fails_pending():
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        task = asyncio.create_task(mgr.rpc_call("dev1", "tools/call", {}, timeout=5))
        await asyncio.sleep(0)
        mgr.unregister("dev1")
        with pytest.raises(DeviceOfflineError):
            await task

    asyncio.run(run())


def test_reconnect_race_old_finally_cannot_kill_new_session():
    """A4.9 C-1 回归：旧连接迟到的 unregister 不得拆掉新连接的会话。"""
    mgr = DeviceSessionManager()
    ws_a, ws_b = FakeWS(), FakeWS()
    sess_a = mgr.register("dev1", "user1", ws_a)
    sess_b = mgr.register("dev1", "user1", ws_b)  # 重连：B 顶替 A
    assert mgr.get("dev1") is sess_b
    mgr.unregister("dev1", sess_a)  # A 的 finally 迟到
    assert mgr.get("dev1") is sess_b, "迟到的旧 finally 拆了新会话"
    assert mgr.is_online("dev1") is True
    mgr.unregister("dev1", sess_b)
    assert mgr.is_online("dev1") is False


def test_stale_session_handle_message_ignored():
    """A4.9 C-1：旧连接的 handle_message 不得改新会话状态。"""
    import datetime

    mgr = DeviceSessionManager()
    sess_a = mgr.register("dev1", "user1", FakeWS())
    sess_b = mgr.register("dev1", "user1", FakeWS())
    sess_b.last_seen = datetime.datetime(2000, 1, 1)
    mgr.handle_message(sess_a, {"method": "heartbeat"})
    assert mgr.get("dev1").last_seen == datetime.datetime(2000, 1, 1)


def test_timeout_sends_tools_cancel():
    """A4.9 I-7：超时时向设备注销在途调用。"""
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        with pytest.raises(DeviceCallTimeoutError):
            await mgr.rpc_call("dev1", "tools/call", {}, timeout=0.05)

    asyncio.run(run())
    assert any(m.get("method") == "tools/cancel" for m in ws.sent)


def test_rpc_expected_user_fences_send():
    """NEW-I2（r4）：send 时原子复核属主——check-then-act 窗闭合。"""
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        return await mgr.rpc_call("dev1", "tools/call", {"x": 1}, timeout=5,
                                  expected_user_id="user2")

    with pytest.raises(DeviceCallError) as ei:
        asyncio.run(run())
    assert getattr(ei.value, "code", None) == "device_owner_mismatch"
    assert not any("method" in p for p in ws.sent), "属主失配不得发出 rpc"


def test_unregister_closes_socket():
    """NEW-M2（r4）：拆会话同时关 socket（僵尸 receive loop 不滞留）。"""
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        mgr.unregister("dev1")

    asyncio.run(run())
    assert {"_closed": 1012} in ws.sent, "unregister 必须关闭 ws"


def test_fetch_bytes_expected_user_fenced():
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        return await mgr.fetch_file_bytes("dev1", "fs/download", {"path": "a"},
                                          expected_user_id="user2")

    with pytest.raises(DeviceCallError) as ei:
        asyncio.run(run())
    assert getattr(ei.value, "code", None) == "device_owner_mismatch"
