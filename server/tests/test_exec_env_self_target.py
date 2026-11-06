"""W32 Task 1 — 执行环境云端联动：本机设备目标（device:<self>）本地直执行短路。

语义（Consistency Hub）：execution_target=device:<本机设备 id> = 「本地」——
工具在本机执行（与云端把 device:<self> 派发回本机 executor 同落点）。
本地栈服务 turn 时不得走 WS 派发（本机设备注册在云端，本地注册表无会话），
也**不得** fail-closed 报错——应短路为本地执行（返回 None=调用方走本地）。

对照腿（D-8 不放松）：device:<other> 无会话仍 fail-closed；无绑定时自目标不短路。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_exec_env_self_target.py -v
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import device_pairing_service
from app.services.remote_dispatch_router import try_remote_dispatch
from app.tools.registry import registry


class FakeConversation:
    def __init__(self, execution_target):
        self.execution_target = execution_target


class FakeUser:
    id = "uid-012345678ab"
    username = "tester"


def _patch_binding(monkeypatch, device_id):
    monkeypatch.setattr(
        device_pairing_service, "load_binding",
        lambda: {"device_id": device_id, "token": "t", "cloud_url": "https://cloud"},
    )


def test_self_target_short_circuits_to_local(monkeypatch):
    """device:<本机> → None（本地执行），绝不 WS 派发/fail-closed。"""
    _patch_binding(monkeypatch, "dev-self")
    out = asyncio.run(try_remote_dispatch(
        "terminal",
        {"command": "echo hi"},
        {"conversation": FakeConversation("device:dev-self"), "user": FakeUser()},
    ))
    assert out is None, f"自目标必须短路本地执行（None），实际={out!r}"


def test_other_device_still_fail_closed(monkeypatch):
    """device:<其他> 无会话 → fail-closed 错误串（D-8 原样，不落本地）。"""
    _patch_binding(monkeypatch, "dev-self")
    out = asyncio.run(try_remote_dispatch(
        "terminal",
        {"command": "echo hi"},
        {"conversation": FakeConversation("device:dev-other"), "user": FakeUser()},
    ))
    assert out is not None and "error" in str(out), f"非自目标不得短路，实际={out!r}"


def test_no_binding_no_shortcut(monkeypatch):
    """无配对绑定（本机无设备身份）→ 自目标判定不生效，按 fail-closed 原样。"""
    monkeypatch.setattr(device_pairing_service, "load_binding", lambda: None)
    out = asyncio.run(try_remote_dispatch(
        "terminal",
        {"command": "echo hi"},
        {"conversation": FakeConversation("device:dev-self"), "user": FakeUser()},
    ))
    assert out is not None and "error" in str(out), f"无绑定不得短路，实际={out!r}"


def test_registry_dispatch_executes_locally_for_self_target(monkeypatch):
    """集成腿：registry.dispatch 对 device:<self> 真本地执行（终端命令真跑出结果）。"""
    _patch_binding(monkeypatch, "dev-self")
    out = asyncio.run(registry.dispatch(
        "terminal",
        {"command": "echo self-target-ok", "_permission_granted": True},
        conversation=FakeConversation("device:dev-self"),
        user=FakeUser(),
    ))
    assert "self-target-ok" in str(out), f"本地执行应产出命令结果，实际={out!r}"
