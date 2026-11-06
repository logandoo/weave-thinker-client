"""W16 设备执行模式 — registry.dispatch 单缝集成腿（判据 #2 核心证据）。

全真链路：registry.dispatch → remote_dispatch_router → DeviceSessionManager
→ 假 WS（捕获 tools/call）→ handle_message 回灌 result → dispatch 还原结果。
覆盖：远端白名单路由 · 服务端工具留本地 · fail-closed 拒绝 · 哨兵剥离 · 离线 fail-fast。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_device_exec_seam.py -v
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.device_session_service import device_session_manager
from app.tools.registry import registry


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000):
        pass


class FakeConversation:
    def __init__(self, execution_target):
        self.execution_target = execution_target


class FakeUser:
    id = "uid-012345678ab"
    username = "tester"


def _wire_device(device_id="dev-seam"):
    ws = FakeWS()
    device_session_manager.register(device_id, FakeUser.id, ws)
    return ws


def _teardown(device_id="dev-seam"):
    device_session_manager.unregister(device_id)


def test_remote_terminal_routes_to_device():
    ws = _wire_device()

    async def run():
        task = asyncio.create_task(
            registry.dispatch(
                "terminal",
                {"command": "ls", "_permission_granted": True},
                conversation=FakeConversation("device:dev-seam"),
                user=FakeUser(),
            )
        )
        await asyncio.sleep(0)
        assert len(ws.sent) == 1
        req = ws.sent[0]
        assert req["method"] == "tools/call"
        assert req["params"]["tool"] == "terminal"
        assert req["params"]["args"] == {"command": "ls"}  # 哨兵已剥离
        assert req["params"]["workspace_ctx"] == "tester-uid-0123"
        device_session_manager.handle_message(
            "dev-seam", {"id": req["id"], "result": {"ok": True, "content": "FROM-DEVICE"}}
        )
        return await task

    try:
        assert asyncio.run(run()) == "FROM-DEVICE"
    finally:
        _teardown()


def test_server_owned_tool_stays_local():
    _wire_device()

    async def run():
        # calculate=服务端拥有型（纯进程内求值）——设备目标下照旧本地执行
        out = await registry.dispatch(
            "calculate",
            {"expression": "1+1"},
            conversation=FakeConversation("device:dev-seam"),
            user=FakeUser(),
        )
        return out

    try:
        out = asyncio.run(run())
        assert "2" in out
    finally:
        _teardown()


def test_unlisted_tool_fails_closed():
    ws = _wire_device()

    async def run():
        return await registry.dispatch(
            "browser_navigate",
            {"url": "http://example.com"},
            conversation=FakeConversation("device:dev-seam"),
            user=FakeUser(),
        )

    try:
        out = asyncio.run(run())
        # A4.9 r2：browser_* = DEFERRED 专属「暂缓」文案（精确钉）
        assert "暂缓" in out
        assert ws.sent == []  # 未打到设备，也未在云端真实执行
    finally:
        _teardown()


def test_offline_device_fail_fast():
    async def run():
        return await registry.dispatch(
            "terminal",
            {"command": "ls"},
            conversation=FakeConversation("device:never-online"),
            user=FakeUser(),
        )

    out = asyncio.run(run())
    assert "离线" in out


def test_cloud_session_ignores_target_none():
    async def run():
        return await registry.dispatch(
            "calculate",
            {"expression": "2+3"},
            conversation=FakeConversation(None),
            user=FakeUser(),
        )

    assert "5" in asyncio.run(run())
