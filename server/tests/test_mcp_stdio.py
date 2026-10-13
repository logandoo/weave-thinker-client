"""Tests for MCP stdio transport in app/tools/mcp_client.py.

A fake MCP stdio server (tests/fixtures/fake_mcp_stdio_server.py) speaks
newline-delimited JSON-RPC over stdin/stdout per the MCP 2024-11-05 stdio
transport spec.

Each test drives its whole flow inside ONE asyncio.run() — asyncio primitives
are loop-bound, and production runs everything on the single uvicorn loop.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

FAKE_SERVER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "fake_mcp_stdio_server.py"
)


@pytest.fixture()
def clean_stdio_state(monkeypatch):
    """Reset stdio session state and idle TTL around each test."""
    import app.tools.mcp_client as mc

    monkeypatch.setattr(mc, "_STDIO_SESSIONS", {})
    monkeypatch.setattr(mc, "_STDIO_SERVER_SPECS", {})
    monkeypatch.setattr(mc, "_STDIO_IDLE_TTL", 60.0)
    yield mc


def test_stdio_registration_registers_tools(clean_stdio_state):
    mc = clean_stdio_state

    async def flow():
        count = await mc.register_mcp_server_async(
            "fakestdio", "", "", command=[sys.executable, FAKE_SERVER]
        )
        entry = mc.registry.get_entry("mcp_fakestdio_echo")
        return count, entry

    count, entry = asyncio.run(flow())
    assert count == 1
    assert entry is not None
    assert entry.toolset == "mcp-fakestdio"


def test_stdio_tool_call_returns_result(clean_stdio_state):
    mc = clean_stdio_state

    async def flow():
        await mc.register_mcp_server_async(
            "fakestdio", "", "", command=[sys.executable, FAKE_SERVER]
        )
        entry = mc.registry.get_entry("mcp_fakestdio_echo")
        assert entry is not None, "handler must be registered"
        return await entry.handler({"message": "hello"})

    payload = json.loads(asyncio.run(flow()))
    assert payload.get("text") == "echo:hello"


def test_stdio_session_reused_across_calls(clean_stdio_state):
    mc = clean_stdio_state

    async def flow():
        await mc.register_mcp_server_async(
            "fakestdio", "", "", command=[sys.executable, FAKE_SERVER]
        )
        entry = mc.registry.get_entry("mcp_fakestdio_echo")
        r1 = json.loads(await entry.handler({"message": "a"}))
        r2 = json.loads(await entry.handler({"message": "b"}))
        return r1, r2

    r1, r2 = asyncio.run(flow())
    assert r1["pid"] == r2["pid"], "same subprocess must serve repeated calls"


def test_stdio_session_restarts_after_process_death(clean_stdio_state):
    mc = clean_stdio_state

    async def flow():
        await mc.register_mcp_server_async(
            "fakestdio", "", "", command=[sys.executable, FAKE_SERVER]
        )
        entry = mc.registry.get_entry("mcp_fakestdio_echo")
        r1 = json.loads(await entry.handler({"message": "a"}))
        session = mc._STDIO_SESSIONS.get("fakestdio")
        assert session is not None
        await session.stop()
        r2 = json.loads(await entry.handler({"message": "b"}))
        return r1, r2

    r1, r2 = asyncio.run(flow())
    assert "error" not in r2
    assert r2["pid"] != r1["pid"], "dead session must be replaced by a fresh process"


def test_stdio_idle_session_reaped(clean_stdio_state, monkeypatch):
    mc = clean_stdio_state
    monkeypatch.setattr(mc, "_cleanup_task", None)

    async def flow():
        await mc.register_mcp_server_async(
            "fakestdio", "", "", command=[sys.executable, FAKE_SERVER]
        )
        # The stdio path must arm the shared cleanup loop on its own (a
        # stdio-only deployment has no HTTP session creation to arm it).
        assert mc._cleanup_task is not None, "cleanup loop must be armed by stdio path"
        session = mc._STDIO_SESSIONS.get("fakestdio")
        assert session is not None
        session.last_used -= mc._STDIO_IDLE_TTL + 1
        await mc._cleanup_expired_sessions()
        for _ in range(40):  # child watcher thread reaps asynchronously
            if session.proc.returncode is not None:
                break
            await asyncio.sleep(0.05)
        return "fakestdio" in mc._STDIO_SESSIONS, session.proc.returncode

    still_cached, returncode = asyncio.run(flow())
    assert not still_cached
    assert returncode is not None, "reaped session must terminate the process"


def test_stdio_failed_start_releases_subprocess(clean_stdio_state, monkeypatch):
    """A session whose initialize handshake fails must not leak the process."""
    mc = clean_stdio_state
    stopped = []

    async def fake_start(self):
        raise RuntimeError("initialize timed out")

    async def fake_stop(self):
        stopped.append(self.name)

    monkeypatch.setattr(mc._StdioSession, "start", fake_start)
    monkeypatch.setattr(mc._StdioSession, "stop", fake_stop)
    mc._STDIO_SERVER_SPECS["fakestdio"] = ([sys.executable, FAKE_SERVER], [], {})

    async def flow():
        with pytest.raises(RuntimeError):
            await mc._get_or_create_stdio_session("fakestdio")

    asyncio.run(flow())
    assert stopped == ["fakestdio"], "failed start must stop the half-started session"
    assert "fakestdio" not in mc._STDIO_SESSIONS


def test_http_url_registration_still_routes_http(clean_stdio_state, monkeypatch):
    """url-only servers must keep using the HTTP path (unchanged behavior)."""
    mc = clean_stdio_state
    called = {}

    async def fake_http_register(name, url, api_key):
        called["http"] = (name, url)
        return 0

    monkeypatch.setattr(mc, "_register_http_server", fake_http_register)
    count = asyncio.run(mc.register_mcp_server_async("srv", "http://example.invalid/mcp", ""))
    assert count == 0
    assert called["http"] == ("srv", "http://example.invalid/mcp")


def test_config_loader_dispatches_command_servers(clean_stdio_state, monkeypatch):
    """load_mcp_servers_from_config_async must route command entries to stdio."""
    mc = clean_stdio_state
    dispatched = {}

    async def fake_register(name, url, api_key, command=None, args=None, env=None):
        dispatched[name] = {"url": url, "command": command, "args": args, "env": env}
        return 1

    monkeypatch.setattr(mc, "register_mcp_server_async", fake_register)
    monkeypatch.setattr(
        mc.config,
        "_config",
        {
            "mcp": {
                "servers": {
                    "local": {
                        "command": "uvx",
                        "args": ["mcp-server-fetch"],
                        "env": {"FAKE_VAR": "1"},
                    },
                    "remote": {"url": "https://mcp.example/mcp"},
                    "broken": {},
                }
            }
        },
    )
    total = asyncio.run(mc.load_mcp_servers_from_config_async())
    assert total == 2
    assert dispatched["local"]["command"] == ["uvx"]
    assert dispatched["local"]["args"] == ["mcp-server-fetch"]
    assert dispatched["local"]["env"] == {"FAKE_VAR": "1"}
    assert dispatched["remote"]["command"] is None
    assert "broken" not in dispatched


def test_stdio_session_respawns_on_new_loop(clean_stdio_state):
    """A session created under loop A must be respawned when used from loop B
    (production analogue: sync startup wrapper loop → main uvicorn loop)."""
    mc = clean_stdio_state

    async def flow_a():
        await mc.register_mcp_server_async(
            "fakestdio", "", "", command=[sys.executable, FAKE_SERVER]
        )
        entry = mc.registry.get_entry("mcp_fakestdio_echo")
        r1 = json.loads(await entry.handler({"message": "a"}))
        return r1

    async def flow_b():
        entry = mc.registry.get_entry("mcp_fakestdio_echo")
        r2 = json.loads(await entry.handler({"message": "b"}))
        return r2

    r1 = asyncio.run(flow_a())
    r2 = asyncio.run(flow_b())
    assert r2["pid"] != r1["pid"], "cross-loop reuse must respawn, not hang"
