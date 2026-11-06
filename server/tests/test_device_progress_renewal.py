"""② progress 续期（2026-10-11）：设备字节通道长打包/转换的 RPC 等待窗。

契约：
- `rpc_call` 收到该 call_id 的 `progress {call_id, note}`（通知面协议已有，
  PLAN_device_execution_mode.md §5）时把等待截止按**原窗**前移——设备存活
  信号换续期；
- 无 progress：超时语义与旧实现逐字节一致（有界性保留，A4.9 I-1）；
- 续期状态在所有出口清理；未知 call_id / 迟到 progress 不复活已结束调用。

Run: cd backend && python -m pytest tests/test_device_progress_renewal.py -q
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.services.device_session_service import (
    DeviceCallTimeoutError,
    DeviceSessionManager,
)


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000):
        self.sent.append({"_closed": code})


def test_progress_renews_deadline():
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        task = asyncio.create_task(
            mgr.rpc_call("dev1", "fs/zip", {"path": "big"}, timeout=0.2)
        )
        await asyncio.sleep(0.01)
        call_id = ws.sent[0]["id"]
        # 0.15s 时发一次 progress → 截止前移（原窗 0.2s）
        await asyncio.sleep(0.14)
        assert not task.done(), "progress 前不应超时"
        mgr.handle_message("dev1", {"method": "progress", "params": {"call_id": call_id, "note": "打包中 30%"}})
        # 再过 0.15s：总时长 0.30s > 原窗 0.2s——若未续期必已超时
        await asyncio.sleep(0.15)
        assert not task.done(), "progress 续期后不得按原截止超时"
        mgr.handle_message("dev1", {"id": call_id, "result": {"ok": True, "data_b64": ""}})
        return await task

    result = asyncio.run(run())
    assert result.get("ok") is True


def test_progress_renewal_is_bounded_without_more_progress():
    """续期只在信号到来时发生：一次 progress 后停发，仍按**新**窗超时。

    判别力：必须活过原窗（elapsed ≥ 原窗+余量）才证明续期发生，且 ≤ 新窗+余量
    证明仍被有界掐断（无续期实现时 elapsed≈原窗 → 断言失败；无界实现 → 超上界）。
    """
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(
            mgr.rpc_call("dev1", "fs/zip", {}, timeout=0.3)
        )
        await asyncio.sleep(0.01)
        call_id = ws.sent[0]["id"]
        await asyncio.sleep(0.14)
        started = loop.time()
        mgr.handle_message("dev1", {"method": "progress", "params": {"call_id": call_id, "note": "50%"}})
        with pytest.raises(DeviceCallTimeoutError):
            await asyncio.wait_for(task, timeout=2.0)
        elapsed = loop.time() - started
        # 新窗 0.3s：续期生效 → ≥0.25；有界 → ≤0.9
        assert elapsed >= 0.25, f"未按新窗存活（续期未生效？elapsed={elapsed:.3f}）"
        assert elapsed <= 0.9, f"未按新窗超时（无界？elapsed={elapsed:.3f}）"

    asyncio.run(run())
    # 超时清理：续期状态不得残留
    sess = mgr.get("dev1")
    assert sess is not None
    assert not sess.pending and not sess.pending_waits


def test_unknown_call_id_progress_ignored():
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        task = asyncio.create_task(mgr.rpc_call("dev1", "fs/zip", {}, timeout=0.1))
        await asyncio.sleep(0.01)
        call_id = ws.sent[0]["id"]
        mgr.handle_message("dev1", {"method": "progress", "params": {"call_id": "bogus", "note": "x"}})
        mgr.handle_message("dev1", {"method": "progress", "params": {"note": "no call id"}})
        mgr.handle_message("dev1", {"method": "progress", "params": {}})
        mgr.handle_message("dev1", {"id": call_id, "result": {"ok": True}})
        return await task

    result = asyncio.run(run())
    assert result.get("ok") is True


def test_late_progress_does_not_revive_finished_call():
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        task = asyncio.create_task(mgr.rpc_call("dev1", "fs/zip", {}, timeout=0.5))
        await asyncio.sleep(0.01)
        call_id = ws.sent[0]["id"]
        mgr.handle_message("dev1", {"id": call_id, "result": {"ok": True}})
        result = await task
        # 迟到 progress：幽灵续期不得复活调用/不得产生状态
        mgr.handle_message("dev1", {"method": "progress", "params": {"call_id": call_id, "note": "late"}})
        sess = mgr.get("dev1")
        assert call_id not in sess.pending_waits
        return result

    result = asyncio.run(run())
    assert result.get("ok") is True


def test_cancel_and_disconnect_clean_renewal_state():
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        task = asyncio.create_task(mgr.rpc_call("dev1", "fs/zip", {}, timeout=5))
        await asyncio.sleep(0.01)
        call_id = ws.sent[0]["id"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        sess = mgr.get("dev1")
        assert call_id not in sess.pending_waits

    asyncio.run(run())
    # 断线：在途调用清表
    async def run2():
        task = asyncio.create_task(mgr.rpc_call("dev1", "fs/zip", {}, timeout=5))
        await asyncio.sleep(0.01)
        mgr.unregister("dev1")
        with pytest.raises(Exception):
            await task

    asyncio.run(run2())


def test_malformed_notification_does_not_kill_session():
    """A4.9 r1 #1 回归：畸形 progress/chunk 帧（params 非 dict）不得抛异常掀翻
    会话（旧行为 inert；新分支必须类型守卫）。"""
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        task = asyncio.create_task(mgr.rpc_call("dev1", "fs/zip", {}, timeout=0.5))
        await asyncio.sleep(0.01)
        call_id = ws.sent[0]["id"]
        # 不得抛（旧实现 AttributeError → device_ws 会 unregister 杀掉在途调用）
        mgr.handle_message("dev1", {"method": "progress", "params": "not-a-dict"})
        mgr.handle_message("dev1", {"method": "progress", "params": None})
        mgr.handle_message("dev1", {"method": "chunk", "params": 123})
        assert mgr.is_online("dev1")
        mgr.handle_message("dev1", {"id": call_id, "result": {"ok": True}})
        return await task

    assert asyncio.run(run()).get("ok") is True


def test_progress_renewal_absolute_cap():
    """D1（遗留清零）：协同式续期有绝对上限（cap）——持续 progress 也不得越界。"""
    from app.core.config import get_config
    cfg = get_config()
    ce = cfg._config.setdefault("client_exec", {})
    old_cap = ce.get("call_max_extension_seconds")
    ce["call_max_extension_seconds"] = 0.25
    try:
        mgr = DeviceSessionManager()
        ws = FakeWS()
        mgr.register("dev1", "user1", ws)

        async def run():
            loop = asyncio.get_running_loop()
            task = asyncio.create_task(mgr.rpc_call("dev1", "fs/zip", {}, timeout=0.15))
            await asyncio.sleep(0.01)
            call_id = ws.sent[0]["id"]
            started = loop.time()

            async def _sender():  # 持续发 progress 直到调用结束（无 cap 则永不超时）
                while not task.done():
                    await asyncio.sleep(0.05)
                    mgr.handle_message("dev1", {"method": "progress", "params": {"call_id": call_id, "note": "chunk"}})

            sender = asyncio.create_task(_sender())
            try:
                with pytest.raises(DeviceCallTimeoutError):
                    await asyncio.wait_for(task, timeout=2.0)
            finally:
                sender.cancel()
                try:
                    await sender
                except asyncio.CancelledError:
                    pass
            elapsed = loop.time() - started
            assert elapsed <= 0.7, f"cap=0.25s 未被遵守（elapsed={elapsed:.3f}）"
            assert elapsed >= 0.18, f"低于原窗/过早掐断（elapsed={elapsed:.3f}）"

        asyncio.run(run())
    finally:
        if old_cap is None:
            ce.pop("call_max_extension_seconds", None)
        else:
            ce["call_max_extension_seconds"] = old_cap


def test_cap_invalid_values_fall_back_to_default():
    """B-F4：非法 cap 值（NaN/负/非数）不得静默关闭硬界——回落 3600；0=显式关。"""
    from app.core.config import get_config

    mgr = DeviceSessionManager()
    cfg = get_config()
    ce = cfg._config.setdefault("client_exec", {})
    old_cap = ce.get("call_max_extension_seconds")
    try:
        for bad in (float("nan"), float("inf"), -5, "abc"):
            ce["call_max_extension_seconds"] = bad
            assert mgr._call_max_extension_seconds() == 3600.0, bad
        ce["call_max_extension_seconds"] = 0
        assert mgr._call_max_extension_seconds() == 0.0
        ce["call_max_extension_seconds"] = 120
        assert mgr._call_max_extension_seconds() == 120.0
    finally:
        if old_cap is None:
            ce.pop("call_max_extension_seconds", None)
        else:
            ce["call_max_extension_seconds"] = old_cap


def test_transfer_assembly_renews_on_chunks(monkeypatch):
    """D2（遗留清零）：分块慢传（总时长超原 idle 窗）——收到块即前移 deadline。"""
    import base64
    import app.services.device_session_service as dss

    monkeypatch.setattr(dss, "TRANSFER_IDLE_EXTRA_SECONDS", 0.3)
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        task = asyncio.create_task(
            mgr.fetch_file_bytes("dev1", "fs/zip", {}, max_bytes=10 ** 6, timeout=0.1)
        )
        await asyncio.sleep(0.01)
        call_id = ws.sent[0]["id"]
        mgr.handle_message("dev1", {"id": call_id, "result": {"ok": True, "transfer_id": "t1"}})
        # idle 窗 = max(0.1,1.0)+0.3 = 1.3s；块在 1.2s（窗前，触发续期→2.5s），
        # 末块 2.4s（越过原窗 1.3s）——无续期则在 ~1.3s 即超时（判别力）。
        await asyncio.sleep(1.2)
        chunk0 = base64.b64encode(b"AB").decode()
        mgr.handle_message("dev1", {"method": "chunk", "params": {"transfer_id": "t1", "seq": 0, "data_b64": chunk0}})
        # 1.5s：越过原截止 1.3s——未续期时 task 已（带异常）结束；存活=续期发生
        await asyncio.sleep(0.3)
        assert not task.done(), "1.2s 的块必须前移截止（原 1.3s 窗）"
        chunk1 = base64.b64encode(b"CD").decode()
        mgr.handle_message("dev1", {"method": "chunk", "params": {"transfer_id": "t1", "seq": 1, "data_b64": chunk1, "last": True}})
        return await task

    data = asyncio.run(run())
    assert data == b"ABCD"


def test_transfer_assembly_stalls_out(monkeypatch):
    """D2 有界：无后续块 → 按 idle 窗超时（不永等）。"""
    import app.services.device_session_service as dss

    monkeypatch.setattr(dss, "TRANSFER_IDLE_EXTRA_SECONDS", 0.3)
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        task = asyncio.create_task(
            mgr.fetch_file_bytes("dev1", "fs/zip", {}, max_bytes=10 ** 6, timeout=0.1)
        )
        await asyncio.sleep(0.01)
        call_id = ws.sent[0]["id"]
        mgr.handle_message("dev1", {"id": call_id, "result": {"ok": True, "transfer_id": "t1"}})
        with pytest.raises(Exception):
            await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(run())


def test_no_progress_timeout_unchanged():
    """旧语义回归：无 progress 的短窗照旧超时。"""
    mgr = DeviceSessionManager()
    ws = FakeWS()
    mgr.register("dev1", "user1", ws)

    async def run():
        with pytest.raises(DeviceCallTimeoutError):
            await mgr.rpc_call("dev1", "fs/zip", {}, timeout=0.05)
        # 超时尽力发 tools/cancel（A4.9 I-7 保持）
        assert any(m.get("method") == "tools/cancel" for m in ws.sent)

    asyncio.run(run())
