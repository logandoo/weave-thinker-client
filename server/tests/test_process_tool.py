"""process 会话（P1b：start/write/read/kill/list）— A4.8 test-first。

覆盖：echo 进程往返、增量 read 不重复、kill、read/list 免权限门控、
start 权限门控（terminal_execution）、会话上限、危险命令拒绝、空闲清理。
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.process_session_service import ProcessSessionManager  # noqa: E402


def _user(uid="u1", perms=None):
    u = SimpleNamespace(id=uid, username="tester")
    u.agent_permissions = perms or {}
    return u


def _patch_workspace(monkeypatch, root: Path):
    async def _ensure(db, user_id, username=None):
        return SimpleNamespace(root_path=str(root))

    monkeypatch.setattr("app.services.workspace_service.ensure_user_workspace", _ensure)


def test_process_echo_roundtrip(tmp_path):
    async def scenario():
        mgr = ProcessSessionManager(max_sessions_per_user=4, idle_timeout_seconds=300)
        try:
            started = await mgr.start(
                "u1",
                "while read line; do echo ECHO:$line; done",
                cwd=str(tmp_path),
                workspace_root=str(tmp_path),
            )
            assert started["ok"], started
            sid = started["session_id"]

            written = await mgr.write("u1", sid, "hello")
            assert written["ok"], written

            buf = ""
            for _ in range(30):
                read = await mgr.read("u1", sid, wait_ms=200)
                buf += read.get("stdout", "")
                if "ECHO:hello" in buf:
                    break
            assert "ECHO:hello" in buf, buf

            killed = await mgr.kill("u1", sid)
            assert killed["ok"]
            listed = mgr.list("u1")
            assert listed["ok"]
        finally:
            await mgr.shutdown_all()

    asyncio.run(scenario())


def test_read_is_incremental(tmp_path):
    async def scenario():
        mgr = ProcessSessionManager(max_sessions_per_user=4, idle_timeout_seconds=300)
        try:
            started = await mgr.start(
                "u1",
                "while read line; do echo OUT:$line; done",
                cwd=str(tmp_path),
                workspace_root=str(tmp_path),
            )
            sid = started["session_id"]
            await mgr.write("u1", sid, "alpha")
            first = await mgr.read("u1", sid, wait_ms=800)
            assert "OUT:alpha" in first.get("stdout", ""), first
            await mgr.write("u1", sid, "beta")
            second = await mgr.read("u1", sid, wait_ms=800)
            assert "OUT:beta" in second.get("stdout", ""), second
            assert "OUT:alpha" not in second.get("stdout", "")
        finally:
            await mgr.shutdown_all()

    asyncio.run(scenario())


def test_session_cap_and_command_validation(tmp_path, monkeypatch):
    import app.tools.terminal as terminal_mod

    def _policy(command):
        if "blocked" in command:
            return {"error": "blocked by policy"}
        return None

    monkeypatch.setattr(terminal_mod, "_validate_command", _policy)

    async def scenario():
        mgr = ProcessSessionManager(max_sessions_per_user=2, idle_timeout_seconds=300)
        try:
            a = await mgr.start("u1", "sleep 30", cwd=str(tmp_path), workspace_root=str(tmp_path))
            b = await mgr.start("u1", "sleep 30", cwd=str(tmp_path), workspace_root=str(tmp_path))
            assert a["ok"] and b["ok"]
            c = await mgr.start("u1", "sleep 30", cwd=str(tmp_path), workspace_root=str(tmp_path))
            assert c["ok"] is False and c.get("error")

            bad = await mgr.start("u2", "blocked cmd", cwd=str(tmp_path), workspace_root=str(tmp_path))
            assert bad["ok"] is False and bad.get("error")

            outside = await mgr.start("u3", "echo hi", cwd="/etc", workspace_root=str(tmp_path))
            assert outside["ok"] is False and outside.get("error")
        finally:
            await mgr.shutdown_all()

    asyncio.run(scenario())


def test_idle_cleanup(tmp_path):
    async def scenario():
        mgr = ProcessSessionManager(max_sessions_per_user=4, idle_timeout_seconds=0.001)
        try:
            started = await mgr.start("u1", "sleep 30", cwd=str(tmp_path), workspace_root=str(tmp_path))
            assert started["ok"]
            await asyncio.sleep(0.05)
            listed = mgr.list("u1")
            assert listed["ok"] and listed["sessions"] == []
        finally:
            await mgr.shutdown_all()

    asyncio.run(scenario())


def test_consume_chars_respects_utf8_boundaries():
    """A4.9 I-5 直接单测：consume_chars 只输出完整字符，半个序列等待下一次 append。"""
    from app.services.process_session_service import _Stream

    stream = _Stream()
    stream.append("中文中文".encode("utf-8"), cap=1024)
    first, _ = stream.consume_chars(2)
    second, _ = stream.consume_chars(2)
    assert first == "中文" and second == "中文"
    assert stream.consume_chars(1)[0] == ""

    partial = _Stream()
    partial.append(b"\xe4\xb8", cap=1024)  # 半个「中」
    assert partial.consume_chars(5)[0] == ""
    partial.append(b"\xad", cap=1024)
    assert partial.consume_chars(5)[0] == "中"


def test_exited_sessions_do_not_accumulate(tmp_path):
    """A4.9 I-3 强化回归（旧代码会失败）：连续短命令不得无界累积会话。"""

    async def scenario():
        mgr = ProcessSessionManager(max_sessions_per_user=2, idle_timeout_seconds=300)
        try:
            for _ in range(5):
                result = await mgr.start("u1", "true", cwd=str(tmp_path), workspace_root=str(tmp_path))
                assert result["ok"], result
                await asyncio.sleep(0.15)
            listed = mgr.list("u1")
            assert len(listed["sessions"]) <= 2, listed
        finally:
            await mgr.shutdown_all()

    asyncio.run(scenario())


def test_exited_sessions_are_reaped_at_cap(tmp_path):
    """A4.9 I-3 回归：上限须覆盖残留的已退出会话（短命令不得无界累积）。"""

    async def scenario():
        mgr = ProcessSessionManager(max_sessions_per_user=1, idle_timeout_seconds=300)
        try:
            a = await mgr.start("u1", "true", cwd=str(tmp_path), workspace_root=str(tmp_path))
            assert a["ok"]
            await asyncio.sleep(0.4)
            b = await mgr.start("u1", "sleep 30", cwd=str(tmp_path), workspace_root=str(tmp_path))
            assert b["ok"], b
        finally:
            await mgr.shutdown_all()

    asyncio.run(scenario())


def test_read_preserves_multibyte_characters(tmp_path):
    """A4.9 I-5 回归：read 不得在 UTF-8 多字节序列中间截断。"""

    async def scenario():
        mgr = ProcessSessionManager(max_sessions_per_user=4, idle_timeout_seconds=300)
        try:
            started = await mgr.start(
                "u1",
                "while read line; do printf '中文字符串中文'; done",
                cwd=str(tmp_path),
                workspace_root=str(tmp_path),
                wait_ms=0,
            )
            assert started["ok"], started
            sid = started["session_id"]
            await mgr.write("u1", sid, "go")

            collected = ""
            for _ in range(8):
                piece = await mgr.read("u1", sid, wait_ms=600, max_chars=2)
                collected += piece.get("stdout", "")
                if len(collected) >= 7:
                    break
            assert "\ufffd" not in collected, repr(collected)
            assert collected == "中文字符串中文", repr(collected)
            await mgr.kill("u1", sid)
        finally:
            await mgr.shutdown_all()

    asyncio.run(scenario())


def test_start_delegates_outside_path_check(tmp_path, monkeypatch):
    """A4.9 I-4 回归：terminal 的运行时路径包含检查同样贯穿 process。"""
    import app.tools.terminal as terminal_mod

    monkeypatch.setattr(
        terminal_mod,
        "_command_accesses_outside_workspace",
        lambda command, cwd, workspace: "/etc/passwd",
    )

    async def scenario():
        mgr = ProcessSessionManager(max_sessions_per_user=4, idle_timeout_seconds=300)
        try:
            result = await mgr.start("u1", "echo hi", cwd=str(tmp_path), workspace_root=str(tmp_path))
            assert result["ok"] is False and "/etc/passwd" in result["error"]
        finally:
            await mgr.shutdown_all()

    asyncio.run(scenario())


def test_process_check_fn_matches_terminal_switch():
    """A4.9 I-4：terminal 主开关关闭时 process 一并隐藏（check_fn 接线）。"""
    from app.tools.registry import registry
    from app.tools.terminal import check_terminal_requirements

    entry = registry.get_entry("process")
    assert entry is not None
    assert entry.check_fn is check_terminal_requirements


def test_process_tool_permission_gate_and_read_free(tmp_path, monkeypatch):
    _patch_workspace(monkeypatch, tmp_path)
    from app.services.process_session_service import process_manager
    from app.tools.process_tool import process

    denied = json.loads(
        asyncio.run(process({"action": "start", "command": "echo hi"}, user=_user(), db=object()))
    )
    assert denied.get("_permission_needed") is True
    assert denied.get("_permission_key") == "terminal_execution"

    async def scenario():
        granted_user = _user(perms={"terminal_execution": True})
        started = json.loads(
            await process({"action": "start", "command": "echo hi"}, user=granted_user, db=object())
        )
        assert started["success"] is True, started
        sid = started["session_id"]
        try:
            read = json.loads(
                await process({"action": "read", "session_id": sid}, user=_user(), db=object())
            )
            assert read.get("_permission_needed") is not True
            assert read["success"] is True
        finally:
            await process_manager.shutdown_all()

    asyncio.run(scenario())
