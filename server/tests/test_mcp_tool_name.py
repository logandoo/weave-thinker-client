"""MCP tool-name sanitization — wave3 2026-09-02.

OpenAI/DeepSeek contract: tool function name must match ^[a-zA-Z0-9_-]+$.
remote MCP servers expose dotted namespaced tools (remote.local_shell.open),
so the composed registry name mcp_<server>_<tool> MUST be sanitized — dispatch
still routes via the handler's original tool_name, only the exposed name changes.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

from app.tools import mcp_client  # noqa: E402
from app.tools.registry import registry as reg  # noqa: E402


class _FakeSession:
    def __init__(self, tools):
        self._tools = tools

    async def _request(self, method, params, timeout=None):
        assert method == "tools/list"
        return {"result": {"tools": self._tools}}

    async def stop(self):
        pass


def _register_with(monkeypatch, tools, server="fake", count_prefix=""):
    return asyncio.run(_register_with_async(monkeypatch, tools, server))


async def _register_with_async(monkeypatch, tools, server="fake"):
    names_before = set(reg.get_all_tool_names())
    async def _fake_session(name):
        return _FakeSession(tools)

    monkeypatch.setattr(mcp_client, "_get_or_create_stdio_session", _fake_session)
    n = await mcp_client._register_stdio_server(server, ["/bin/echo", "serve"])
    monkeypatch.undo()
    names = [x for x in reg.get_all_tool_names() if x not in names_before]
    # cleanup registered entries to keep tests isolated
    for x in names:
        reg.unregister(x)
    assert n == len(tools), (n, len(tools))
    return names


def test_dotted_tool_name_sanitized(monkeypatch):
    names = _register_with(
        monkeypatch, [{"name": "remote.local_shell.open"}], server="t1"
    )
    assert names == ["mcp_t1_remote_local_shell_open"]


def test_clean_name_untouched(monkeypatch):
    names = _register_with(monkeypatch, [{"name": "plain_tool"}], server="t2")
    assert names == ["mcp_t2_plain_tool"]


def test_collision_gets_deterministic_suffix(monkeypatch):
    names = _register_with(
        monkeypatch,
        [{"name": "a.b"}, {"name": "a_b"}],
        server="t3",
    )
    assert sorted(names) == ["mcp_t3_a_b", "mcp_t3_a_b_2"]


# ── HTTP (Streamable) path — conv 6dcae019 2026-09-03: sanitization existed
#    only on the stdio path; the remote switch to Streamable HTTP re-exposed
#    the dotted-name 400 (Invalid 'tools[27].function.name'). ──

def _register_http_with(monkeypatch, tools, server="h1"):
    from app.tools import mcp_client as mc

    names_before = set(reg.get_all_tool_names())

    async def _fake_init(url, headers, **kwargs):  # 2026-11-02: 新增 owner kwarg
        return "session-1"

    async def _fake_list(url, session_id, headers, **kwargs):  # 2026-11-02: 新增 owner kwarg
        return tools

    monkeypatch.setattr(mc, "_mcp_initialize", _fake_init)
    monkeypatch.setattr(mc, "_mcp_list_tools", _fake_list)
    n = asyncio.run(mc._register_http_server(server, "http://fake/mcp", ""))
    monkeypatch.undo()
    names = [x for x in reg.get_all_tool_names() if x not in names_before]
    for x in names:
        reg.unregister(x)
    assert n == len(tools), (n, len(tools))
    return names


def test_http_dotted_tool_name_sanitized(monkeypatch):
    names = _register_http_with(
        monkeypatch, [{"name": "remote.hosts.4090.nvidia-smi"}], server="h1"
    )
    assert names == ["mcp_h1_remote_hosts_4090_nvidia-smi"], names
    assert all(_NAME_RE2.match(x) for x in names), names


def test_http_clean_name_and_collision(monkeypatch):
    names = _register_http_with(
        monkeypatch, [{"name": "plain"}, {"name": "plain"}], server="h2"
    )
    assert sorted(names) == ["mcp_h2_plain", "mcp_h2_plain_2"], names


def test_sync_register_sanitized(monkeypatch):
    import asyncio as _aio

    async def _fake_list(url, session_id, headers, **kwargs):  # 2026-11-02: 新增 owner kwarg
        return [{"name": "remote.local_shell.open"}]

    async def _fake_init(url, headers, **kwargs):  # 2026-11-02: 新增 owner kwarg
        return "s"

    monkeypatch.setattr(mcp_client, "_mcp_initialize", _fake_init)
    monkeypatch.setattr(mcp_client, "_mcp_list_tools", _fake_list)
    names_before = set(reg.get_all_tool_names())
    n = asyncio.run(mcp_client._register_mcp_server_sync("s1", "http://fake/mcp", ""))
    monkeypatch.undo()
    names = [x for x in reg.get_all_tool_names() if x not in names_before]
    for x in names:
        reg.unregister(x)
    assert n == 1 and names == ["mcp_s1_remote_local_shell_open"], names


import re as _re2
_NAME_RE2 = _re2.compile(r"^[a-zA-Z0-9_-]+$")
