"""AgentLoop MCP 渐进发现（延迟池 + search_tools 追加 + 历史预载）单测。

用独立 server 名 progtest 在全局 registry 注册假 MCP 工具；断言：
1) progressive 开 → schemas 不含任何 mcp_* 且含 search_tools
2) progressive 关 → 回滚到全量
3) [mcp.servers.X] eager=true → 该 server 工具留在 schemas
4) 显式 tool_schemas（子代理）→ 完全不动
5) search_tools 结果 → 追加 matched schemas + _active_tool_names（幂等）
6) 历史含 mcp_* 调用 → run 前预载
7) 池空 → schemas 剔除 search_tools
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from app.core.config import get_config  # noqa: E402
from app.tools import registry  # noqa: E402
from app.services.agent_loop import AgentLoop  # noqa: E402

config = get_config()

_FAKES = {
    "mcp_progtest_remote_shell_open": ("mcp-progtest", "Open a remote shell"),
    "mcp_progtest_sftp_upload": ("mcp-progtest", "Upload a file via sftp"),
    "mcp_progtestE_host_list": ("mcp-progtestE", "List hosts"),
}


def _register_fakes():
    for name, (toolset, desc) in _FAKES.items():
        registry.register(
            name=name, toolset=toolset,
            schema={"name": name, "description": desc,
                    "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **kw: "{}", is_async=False, description=desc,
        )


def _unregister_fakes():
    for name in _FAKES:
        registry.unregister(name)


def _schema_names(loop):
    return [(s.get("function") or {}).get("name") for s in loop.tool_schemas]


def _fresh_loop(**kw):
    return AgentLoop(llm=object(), **kw)


def test_progressive_defers_mcp_and_keeps_search_tools():
    _register_fakes()
    try:
        loop = _fresh_loop()
        names = _schema_names(loop)
        assert not any(n and n.startswith("mcp_") for n in names), \
            "MCP schemas must be deferred"
        assert "search_tools" in names
        assert "web_search" in names
    finally:
        _unregister_fakes()


def test_progressive_off_restores_all():
    _register_fakes()
    try:
        cls = type(config)
        monkey = pytest.MonkeyPatch()
        monkey.setattr(cls, "agent_tools_mcp_progressive_enabled",
                       property(lambda self: False))
        try:
            loop = _fresh_loop()
            assert "mcp_progtest_remote_shell_open" in _schema_names(loop)
        finally:
            monkey.undo()
    finally:
        _unregister_fakes()


def test_eager_server_kept():
    _register_fakes()
    try:
        servers = config.mcp.setdefault("servers", {})
        saved = dict(servers.get("progtest", {}))
        servers["progtest"] = {**saved, "eager": True}
        try:
            loop = _fresh_loop()
            names = _schema_names(loop)
            assert "mcp_progtest_remote_shell_open" in names
            assert "mcp_progtestE_host_list" not in names
        finally:
            servers["progtest"] = saved
    finally:
        _unregister_fakes()


def test_explicit_schemas_untouched():
    explicit = [{"type": "function", "function": {
        "name": "mcp_progtest_remote_shell_open",
        "description": "x", "parameters": {"type": "object"}}}]
    loop = _fresh_loop(tool_schemas=explicit)
    assert _schema_names(loop) == ["mcp_progtest_remote_shell_open"]


def test_expand_after_search_result_idempotent():
    _register_fakes()
    try:
        loop = _fresh_loop()
        assert "mcp_progtest_sftp_upload" not in _schema_names(loop)
        from app.services.agent_loop import AgentLoopState
        state = AgentLoopState()
        state.tool_results.append(type(
            "TR", (), {"call_id": "c1", "name": "search_tools", "arguments": {},
                       "result": json.dumps({"matches": [
                           {"name": "mcp_progtest_sftp_upload",
                            "description": "Upload a file via sftp",
                            "server": "progtest"}]},
                                          ensure_ascii=False), "error": False})()
        )
        loop._maybe_expand_searched_tools(state)
        assert "mcp_progtest_sftp_upload" in _schema_names(loop)
        assert "mcp_progtest_sftp_upload" in loop._active_tool_names
        n = len(loop.tool_schemas)
        loop._maybe_expand_searched_tools(state)
        assert len(loop.tool_schemas) == n, "must be idempotent (only-grow)"
    finally:
        _unregister_fakes()


def test_history_preload():
    _register_fakes()
    try:
        loop = _fresh_loop()
        assert "mcp_progtest_remote_shell_open" not in _schema_names(loop)
        msgs = [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_x", "type": "function",
             "function": {"name": "mcp_progtest_remote_shell_open",
                          "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_x",
             "name": "mcp_progtest_remote_shell_open", "content": "ok"}]
        loop._preload_discovered_from_history(msgs)
        assert "mcp_progtest_remote_shell_open" in _schema_names(loop)
    finally:
        _unregister_fakes()


def test_empty_pool_drops_search_tools():
    mcp_names = [n for n, ts in registry.get_tool_to_toolset_map().items()
                 if ts and ts.startswith("mcp-")]
    saved = {}
    try:
        for n in mcp_names:
            e = registry.get_entry(n)
            saved[n] = (e.toolset, e.schema, e.handler, e.description, e.is_async,
                        e.check_fn, list(e.requires_env or []))
            registry.unregister(n)
        loop = _fresh_loop()
        assert "search_tools" not in _schema_names(loop)
    finally:
        for n, (toolset, schema, handler, desc, is_async, check_fn, req) in saved.items():
            registry.register(name=n, toolset=toolset, schema=schema,
                              handler=handler, is_async=is_async, description=desc,
                              check_fn=check_fn, requires_env=req)
