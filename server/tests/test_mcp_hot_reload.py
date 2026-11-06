"""TDD — global MCP config hot reload (2026-11-01).

Contract:
  R1  Adding a `[mcp.servers.*]` entry and reloading registers its tools
      without a process restart.
  R2  Removing an entry unregisters its tools AND stops its stdio session.
  R3  Changing a server spec re-registers it (old tools gone, new tools usable).
  R4  A malformed config keeps the old tools (probe-then-swap / fail-safe).
  R5  User-owned MCP tools are never touched by a global reload.

Run: cd backend && python -m pytest tests/test_mcp_hot_reload.py -v
"""
import asyncio
import os
import sys

import pytest

FAKE_SERVER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "fake_mcp_stdio_server.py"
)


def _write_config(path, servers: dict) -> None:
    lines = []
    for name, spec in servers.items():
        lines.append(f"[mcp.servers.{name}]")
        if spec.get("command"):
            cmd = spec["command"]
            if isinstance(cmd, (list, tuple)):
                lines.append("command = [" + ", ".join(repr(x) for x in cmd) + "]")
            else:
                lines.append(f"command = {cmd!r}")
        if spec.get("args"):
            lines.append("args = [" + ", ".join(repr(x) for x in spec["args"]) + "]")
        if spec.get("url"):
            lines.append(f"url = {spec['url']!r}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture()
def clean_state(monkeypatch):
    import app.tools.mcp_client as mc

    monkeypatch.setattr(mc, "_GLOBAL_MCP_STATE", {}, raising=False)
    monkeypatch.setattr(mc, "_STDIO_SESSIONS", {})
    monkeypatch.setattr(mc, "_STDIO_SERVER_SPECS", {})
    monkeypatch.setattr(mc, "_STDIO_IDLE_TTL", 60.0)
    # Remove any tools left by a previous test/process for these names.
    for name in ("mcp_hot1_echo", "mcp_hot2_echo", "mcp_hot3_echo"):
        mc.registry.unregister(name)
    yield mc
    for name in ("mcp_hot1_echo", "mcp_hot2_echo", "mcp_hot3_echo"):
        mc.registry.unregister(name)


def test_add_reload_registers_tools_without_restart(clean_state, tmp_path):
    mc = clean_state
    cfg = tmp_path / "config.toml"
    _write_config(cfg, {"hot1": {"command": [sys.executable, FAKE_SERVER]}})

    async def flow():
        result = await mc.reload_mcp_servers_from_config(str(cfg))
        entry = mc.registry.get_entry("mcp_hot1_echo")
        out = await entry.handler({"message": "x"}) if entry else None
        return result, entry, out

    result, entry, out = asyncio.run(flow())
    assert result["ok"] is True
    assert entry is not None and entry.toolset == "mcp-hot1"
    assert "echo:x" in (out or "")


def test_remove_reload_unregisters_and_stops_session(clean_state, tmp_path):
    mc = clean_state
    cfg = tmp_path / "config.toml"
    _write_config(cfg, {"hot1": {"command": [sys.executable, FAKE_SERVER]}})

    async def flow():
        await mc.reload_mcp_servers_from_config(str(cfg))
        old_session = mc._STDIO_SESSIONS.get("hot1")
        assert old_session is not None
        _write_config(cfg, {})
        result = await mc.reload_mcp_servers_from_config(str(cfg))
        return result, old_session

    result, old_session = asyncio.run(flow())
    assert result["ok"] is True
    assert mc.registry.get_entry("mcp_hot1_echo") is None
    assert old_session is not None and old_session.proc is not None
    assert old_session.proc.returncode is not None, "old stdio subprocess must be stopped"


def test_changed_spec_replaces_tools(clean_state, tmp_path):
    mc = clean_state
    cfg = tmp_path / "config.toml"
    _write_config(cfg, {"hot1": {"command": [sys.executable, FAKE_SERVER]}})

    async def flow():
        await mc.reload_mcp_servers_from_config(str(cfg))
        # Same tool name, different server identity → old registration must be
        # dropped before the new one (no stale duplicate handler).
        _write_config(cfg, {"hot2": {"command": [sys.executable, FAKE_SERVER]}})
        result = await mc.reload_mcp_servers_from_config(str(cfg))
        return result

    result = asyncio.run(flow())
    assert result["ok"] is True
    assert mc.registry.get_entry("mcp_hot1_echo") is None
    assert mc.registry.get_entry("mcp_hot2_echo") is not None


def test_malformed_config_keeps_old_tools(clean_state, tmp_path):
    mc = clean_state
    cfg = tmp_path / "config.toml"
    _write_config(cfg, {"hot1": {"command": [sys.executable, FAKE_SERVER]}})

    async def flow():
        await mc.reload_mcp_servers_from_config(str(cfg))
        cfg.write_text("[mcp.servers.hot3\ncommand = nope", encoding="utf-8")
        result = await mc.reload_mcp_servers_from_config(str(cfg))
        return result

    result = asyncio.run(flow())
    assert result["ok"] is False
    assert result.get("errors")
    assert mc.registry.get_entry("mcp_hot1_echo") is not None


def test_global_reload_never_touches_user_owned_tools(clean_state, tmp_path):
    mc = clean_state
    mc.registry.register(
        name="mcp_userowned_probe",
        toolset="mcp-hotuser",
        schema={"name": "mcp_userowned_probe", "parameters": {"type": "object"}},
        handler=lambda args, **kw: "{}",
        description="user-owned probe",
        owner_user_id="user-1",
    )
    cfg = tmp_path / "config.toml"
    _write_config(cfg, {"hot1": {"command": [sys.executable, FAKE_SERVER]}})
    try:
        asyncio.run(mc.reload_mcp_servers_from_config(str(cfg)))
        assert mc.registry.get_entry("mcp_userowned_probe") is not None
        assert mc.registry.get_all_registered_names().count("mcp_userowned_probe") == 1
    finally:
        mc.registry.unregister("mcp_userowned_probe")
