"""F1 RED tests — 沙箱共享浏览器注册表（PLAYWRIGHT_BROWSERS_PATH 固定到服务端注册表）。

背景：terminal/code_execution/process 三处沙箱 env 把 HOME 重定向进用户工作区
（隔离），playwright 按 $HOME 解析浏览器注册表 → 每工作区重复下载 641M 浏览器
（60 生产实测 3 份）。F1：三处 env 注入 PLAYWRIGHT_BROWSERS_PATH=服务端共享注册表。

Run from backend/:  python -m pytest tests/test_sandbox_env_f1.py -v
"""
import pytest

from app.services.sandbox_env import (
    BROWSERS_ENV_VAR,
    resolve_shared_browsers_path,
)


class TestResolve:
    def test_platform_default_posix(self):
        p = resolve_shared_browsers_path(None, environ={}, home="/root", platform="linux")
        assert p == "/root/.cache/ms-playwright"

    def test_platform_default_darwin(self):
        p = resolve_shared_browsers_path(None, environ={}, home="/Users/x", platform="darwin")
        assert p == "/Users/x/Library/Caches/ms-playwright"

    def test_service_env_takes_precedence_over_default(self):
        p = resolve_shared_browsers_path(
            None, environ={"PLAYWRIGHT_BROWSERS_PATH": "/srv/pw"}, home="/root", platform="linux"
        )
        assert p == "/srv/pw"

    def test_config_override_beats_service_env(self):
        p = resolve_shared_browsers_path(
            "/custom/pw", environ={"PLAYWRIGHT_BROWSERS_PATH": "/srv/pw"}, home="/root", platform="linux"
        )
        assert p == "/custom/pw"

    def test_off_disables_injection(self):
        p = resolve_shared_browsers_path("off", environ={}, home="/root", platform="linux")
        assert p is None

    def test_empty_config_is_auto(self):
        p = resolve_shared_browsers_path("", environ={}, home="/root", platform="linux")
        assert p == "/root/.cache/ms-playwright"


class TestWiring:
    def test_terminal_sandbox_env_pins_browsers(self):
        from app.tools.terminal import _sandbox_env
        env = _sandbox_env("/usr/bin:/bin", "/tmp/ws", {})
        assert env["HOME"] == "/tmp/ws"
        assert BROWSERS_ENV_VAR in env
        assert env[BROWSERS_ENV_VAR].endswith("ms-playwright")

    def test_process_build_env_pins_browsers(self):
        from app.services.process_session_service import process_manager
        env = process_manager._build_env("/tmp/ws")
        assert env["HOME"] == "/tmp/ws"
        assert BROWSERS_ENV_VAR in env

    def test_code_execution_build_env_pins_browsers(self):
        from app.services.code_execution_service import _build_env
        env = _build_env("/tmp/ws", None)
        assert env["HOME"] == "/tmp/ws"
        assert BROWSERS_ENV_VAR in env

    def test_playwright_env_wrapper_reads_config(self):
        from app.services.sandbox_env import playwright_browsers_env
        d = playwright_browsers_env()
        assert isinstance(d, dict)
        if d:
            assert set(d) == {BROWSERS_ENV_VAR}
            assert isinstance(d[BROWSERS_ENV_VAR], str) and d[BROWSERS_ENV_VAR]


class TestA49FixRound:
    """评审 fix-round（M1 相对路径 / off 大小写 / wrapper 确定性 / I1 接线）。"""

    def test_relative_path_normalized_to_absolute(self):
        import os
        p = resolve_shared_browsers_path("rel/path", environ={}, home="/root", platform="linux")
        assert os.path.isabs(p) and p.endswith("rel/path")

    def test_env_relative_path_normalized(self):
        import os
        p = resolve_shared_browsers_path(
            None, environ={"PLAYWRIGHT_BROWSERS_PATH": "rel/pw"}, home="/root", platform="linux"
        )
        assert os.path.isabs(p)

    def test_off_is_case_insensitive(self):
        assert resolve_shared_browsers_path("OFF", environ={}, home="/root", platform="linux") is None

    def test_wrapper_off_returns_empty(self, monkeypatch):
        import app.core.config as cfg_mod
        from app.services import sandbox_env as se

        class _Cfg:
            browser_shared_browsers_path = "off"

        monkeypatch.setattr(cfg_mod, "get_config", lambda: _Cfg())
        assert se.playwright_browsers_env() == {}

    def test_wrapper_explicit_path_deterministic(self, monkeypatch):
        import app.core.config as cfg_mod
        from app.services import sandbox_env as se

        class _Cfg:
            browser_shared_breadcrumbs = None
            browser_shared_browsers_path = "/srv/pw"

        monkeypatch.setattr(cfg_mod, "get_config", lambda: _Cfg())
        assert se.playwright_browsers_env() == {"PLAYWRIGHT_BROWSERS_PATH": "/srv/pw"}

    def test_code_execution_wiring_registers_known_cmdline(self, monkeypatch):
        """I1/I2 接线行为测试：真实 spawn → register 收到已知命令串 + source。"""
        import asyncio
        from pathlib import Path

        from app.core.config import get_config
        from app.services import spawn_registry as sr_mod
        from app.services.code_execution_service import CodeExecutionService

        ws = Path(get_config().workspace_root) / "f1f2_wiring_test"
        ws.mkdir(parents=True, exist_ok=True)
        seen = []
        monkeypatch.setattr(
            sr_mod, "register",
            lambda pgid, cmdline, cwd, source: seen.append((pgid, cmdline, source)),
        )
        monkeypatch.setattr(sr_mod, "unregister_if_empty", lambda pgid: None)
        res = asyncio.run(CodeExecutionService().execute_python("print('wired')", cwd=str(ws)))
        assert "wired" in res.stdout
        assert seen, "spawn 未登记（F2 接线缺失）"
        _pgid, cmdline, source = seen[0]
        assert source == "code_execution"
        assert cmdline and "print" in cmdline  # 已知命令串，而非空签名
