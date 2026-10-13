"""search_tools 元工具（MCP 渐进发现）单测。

注册名 search_tools / toolset core；扫描 registry 中 toolset 前缀 mcp- 的
条目，query 词与 name token / description 匹配（CJK 子串通道），按相关度
排序取 top-limit。使用独立 server 名 progtestA/B 避免污染全局 registry。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.tools import registry  # noqa: E402
from app.tools.mcp_search import search_tools_handler  # noqa: E402

_FAKE = [
    ("mcp_progtestA_remote_shell_open", "mcp-progtestA",
     {"name": "mcp_progtestA_remote_shell_open",
      "description": "Open an interactive remote shell session",
      "parameters": {"type": "object", "properties": {}}}, "打开远程 shell"),
    ("mcp_progtestA_remote_shell_write", "mcp-progtestA",
     {"name": "mcp_progtestA_remote_shell_write",
      "description": "Write bytes into the remote shell",
      "parameters": {"type": "object", "properties": {}}}, "写入远程 shell"),
    ("mcp_progtestA_port_forward_create", "mcp-progtestA",
     {"name": "mcp_progtestA_port_forward_create",
      "description": "Create a port forwarding rule",
      "parameters": {"type": "object", "properties": {}}}, "用于端口转发配置管理"),
    ("mcp_progtestB_host_list", "mcp-progtestB",
     {"name": "mcp_progtestB_host_list",
      "description": "List saved hosts",
      "parameters": {"type": "object", "properties": {}}}, "列出已保存主机"),
]


def _setup():
    for name, toolset, schema, desc in _FAKE:
        registry.register(
            name=name, toolset=toolset, schema=schema,
            handler=lambda args, **kw: "{}", is_async=False, description=desc,
        )


def _teardown():
    for name, _, _, _ in _FAKE:
        registry.unregister(name)


def _run(args):
    return json.loads(asyncio.run(search_tools_handler(args)))


def test_search_tools_registered_in_core():
    assert registry.get_toolset_for_tool("search_tools") == "core"


def test_match_by_name_tokens_ranked():
    _setup()
    try:
        out = _run({"query": "remote shell"})
        names = [m["name"] for m in out["matches"]]
        assert names[0] == "mcp_progtestA_remote_shell_open", names
        assert "mcp_progtestA_remote_shell_write" in names
        assert "mcp_progtestB_host_list" not in names
    finally:
        _teardown()


def test_match_by_cjk_description_substring():
    _setup()
    try:
        out = _run({"query": "端口转发"})
        names = [m["name"] for m in out["matches"]]
        assert names == ["mcp_progtestA_port_forward_create"], names
    finally:
        _teardown()


def test_limit_and_ranking():
    _setup()
    try:
        out = _run({"query": "shell", "limit": 1})
        assert len(out["matches"]) == 1
        assert out["matches"][0]["name"] == "mcp_progtestA_remote_shell_open"
    finally:
        _teardown()


def test_server_filter():
    _setup()
    try:
        registry.register(
            name="mcp_progtestB_shell_ping", toolset="mcp-progtestB",
            schema={"name": "mcp_progtestB_shell_ping",
                    "description": "ping via shell", "parameters": {"type": "object"}},
            handler=lambda args, **kw: "{}", is_async=False, description="shell ping",
        )
        try:
            out = _run({"query": "shell", "server": "progtestB"})
            names = [m["name"] for m in out["matches"]]
            assert names == ["mcp_progtestB_shell_ping"], names
        finally:
            registry.unregister("mcp_progtestB_shell_ping")
    finally:
        _teardown()


def test_no_match_empty_and_shape():
    _setup()
    try:
        out = _run({"query": "zzz_nonexistent"})
        assert out["matches"] == []
        out2 = _run({"query": "shell"})
        assert set(out2["matches"][0].keys()) >= {"name", "description", "server"}
        assert "loaded_hint" in out2
    finally:
        _teardown()
