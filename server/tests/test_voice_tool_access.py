"""Voice assistant toolset + zh/en tool display-name mapping tests.

Run (project venv):
    .venv/bin/python backend/tests/test_voice_tool_access.py

Covers:
  1. session_search is in the voice whitelist.
  2. All 11 legacy voice tools are still present (no accidental removal).
  3. _voice_tool_names() merges every registered mcp-* tool dynamically.
  4. zh/en display names for known tools (web_search/session_search/...).
  5. MCP display names split via registry toolset — unambiguous even when the
     server name contains underscores.
  6. Unknown tool fallback: underscores -> spaces.
  7. _detect_text_language: zh / en / mixed / empty.
  8. _build_tool_notice_names: spoken names, no raw snake_case in the notice.
  9. _build_voice_tool_results UI titles reuse the same mapping.
"""
import asyncio
import json
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.tools.registry import registry
from app.services.voice_service import (
    _VOICE_TOOLS,
    _voice_tool_names,
    _voice_tool_display_name,
    _detect_text_language,
    _build_tool_notice_names,
    _build_voice_tool_results,
)

LOG_PATH = os.path.join(ROOT, "tests", "voice_tool_access.log")

LEGACY_TOOLS = {
    "web_search", "browser", "execute_code", "memory", "notes", "pdf_export",
    "context7_resolve_library_id", "context7_query_docs", "terminal",
    "skill_view", "background_task",
}


def fake_mcp_tool(name, toolset, description="fake mcp tool"):
    registry.register(
        name=name,
        toolset=toolset,
        schema={"name": name, "description": description, "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kwargs: "ok",
    )


def main():
    passed = 0
    failed = 0

    def check(label, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS {label}")
        else:
            failed += 1
            print(f"  FAIL {label} {detail}")

    print("== 1. whitelist ==")
    check("session_search in _VOICE_TOOLS", "session_search" in _VOICE_TOOLS)
    check("legacy 11 tools intact", LEGACY_TOOLS <= _VOICE_TOOLS,
          f"missing={LEGACY_TOOLS - _VOICE_TOOLS}")
    check("exactly 12 static tools", len(_VOICE_TOOLS) == 12,
          f"count={len(_VOICE_TOOLS)}")

    print("== 2. dynamic MCP merge ==")
    fake_mcp_tool("mcp_my_server_find_file", "mcp-my_server")
    try:
        names = _voice_tool_names()
        check("mcp tool merged", "mcp_my_server_find_file" in names)
        check("whitelist still all present", LEGACY_TOOLS | {"session_search"} <= names)
        check("no non-MCP extras leaked", names - (LEGACY_TOOLS | {"session_search", "mcp_my_server_find_file"}) == set(),
              f"extra={names - (LEGACY_TOOLS | {'session_search', 'mcp_my_server_find_file'})}")
    finally:
        registry.unregister("mcp_my_server_find_file")
    check("mcp tool removed after unregister", "mcp_my_server_find_file" not in _voice_tool_names())

    print("== 3. known display names ==")
    check("web_search zh", _voice_tool_display_name("web_search", "zh") == "联网查询")
    check("web_search en", _voice_tool_display_name("web_search", "en") == "web search")
    check("session_search zh", _voice_tool_display_name("session_search", "zh") == "历史会话检索")
    check("session_search en", _voice_tool_display_name("session_search", "en") == "search conversation history")
    check("browser zh", _voice_tool_display_name("browser", "zh") == "浏览网页")
    check("background_task zh", _voice_tool_display_name("background_task", "zh") == "后台任务")
    check("pdf_export en", _voice_tool_display_name("pdf_export", "en") == "export PDF")
    check("terminal en", _voice_tool_display_name("terminal", "en") == "terminal")

    print("== 4. MCP display names via toolset ==")
    fake_mcp_tool("mcp_my_server_find_file", "mcp-my_server")
    try:
        check("mcp zh", _voice_tool_display_name("mcp_my_server_find_file", "zh") == "my_server 的 find file 工具",
              _voice_tool_display_name("mcp_my_server_find_file", "zh"))
        check("mcp en", _voice_tool_display_name("mcp_my_server_find_file", "en") == "my_server find file tool",
              _voice_tool_display_name("mcp_my_server_find_file", "en"))
    finally:
        registry.unregister("mcp_my_server_find_file")
    fake_mcp_tool("mcp_my-server_find", "mcp-my-server")
    try:
        check("mcp hyphen-server zh", _voice_tool_display_name("mcp_my-server_find", "zh") == "my-server 的 find 工具",
              _voice_tool_display_name("mcp_my-server_find", "zh"))
        check("mcp hyphen-server en", _voice_tool_display_name("mcp_my-server_find", "en") == "my-server find tool",
              _voice_tool_display_name("mcp_my-server_find", "en"))
    finally:
        registry.unregister("mcp_my-server_find")

    print("== 4b. MCP merge + check_fn gating in get_definitions ==")
    from unittest.mock import patch

    fake_mcp_tool("mcp_my_server_find_file", "mcp-my_server")
    try:
        defs = registry.get_definitions(_voice_tool_names(), quiet=True)
        names_in_defs = {d["function"]["name"] for d in defs}
        check("session_search in definitions", "session_search" in names_in_defs)
        check("mcp tool in definitions", "mcp_my_server_find_file" in names_in_defs)
    finally:
        registry.unregister("mcp_my_server_find_file")
    from app.tools import session_search as session_search_mod

    entry = registry.get_entry("session_search")
    orig_check = entry.check_fn
    entry.check_fn = lambda: False
    try:
        defs = registry.get_definitions(_voice_tool_names(), quiet=True)
        names_in_defs = {d["function"]["name"] for d in defs}
        check("session_search gated by check_fn", "session_search" not in names_in_defs)
    finally:
        entry.check_fn = orig_check
    defs = registry.get_definitions(_voice_tool_names(), quiet=True)
    check("session_search back when enabled", "session_search" in {d["function"]["name"] for d in defs})

    print("== 5. unknown tool fallback ==")
    check("unknown zh prettify", _voice_tool_display_name("some_weird_tool", "zh") == "some weird tool")
    check("unknown en prettify", _voice_tool_display_name("some_weird_tool", "en") == "some weird tool")
    check("unregistered mcp-shaped name falls to prettify",
          _voice_tool_display_name("mcp_x_y", "zh") == "mcp x y")

    print("== 6. language detection ==")
    check("pure zh -> zh", _detect_text_language("帮我查一下今天的天气怎么样") == "zh")
    check("pure en -> en", _detect_text_language("what is the weather today") == "en")
    check("empty -> zh", _detect_text_language("") == "zh")
    check("whitespace -> zh", _detect_text_language("   ") == "zh")
    check("mixed zh-heavy -> zh", _detect_text_language("用 python 写个脚本") == "zh")
    check("mixed en-heavy -> en", _detect_text_language("write a python script please") == "en")
    check("cjk-punct only -> zh", _detect_text_language("。。！") == "zh")
    check("digits only -> zh", _detect_text_language("12345") == "zh")
    check("emoji only -> zh", _detect_text_language("😀🎉") == "zh")
    check("url-ish -> en", _detect_text_language("check https://example.com/x") == "en")

    print("== 7. notice names (TTS broadcast) ==")
    calls = [
        {"function": {"name": "web_search"}},
        {"function": {"name": "session_search"}},
    ]
    zh_names = _build_tool_notice_names(calls, "zh")
    en_names = _build_tool_notice_names(calls, "en")
    check("zh names joined", zh_names == "联网查询、历史会话检索", zh_names)
    check("en names joined", en_names == "web search, search conversation history", en_names)
    check("no raw snake_case in zh", "web_search" not in zh_names and "session_search" not in zh_names)
    check("no raw snake_case in en", "web_search" not in en_names and "session_search" not in en_names)
    notice_zh = f"(平静)好的，我来调用{zh_names}工具帮你处理。这个任务可能需要较长时间，请耐心等待，我会持续为你工作。"
    notice_en = f"(平静)好的，我来调用{en_names}工具帮你处理。这个任务可能需要较长时间，请耐心等待，我会持续为你工作。"
    check("notice zh contains mapped name", "联网查询" in notice_zh)
    check("notice en contains 'web search'", "web search" in notice_en)
    check("notice never leaks tool_call ids", "web_search" not in notice_zh + notice_en)

    print("== 8. UI tool-call block titles ==")
    fake_mcp_tool("mcp_my_server_find_file", "mcp-my_server")
    try:
        payload = json.loads(_build_voice_tool_results([
            {"name": "session_search", "tool_call_id": "c1", "content": "{}"},
            {"name": "mcp_my_server_find_file", "tool_call_id": "c2", "content": "{}"},
        ]))
        titles = [s["title"] for s in payload["agent_steps"]]
        check("UI title session_search", titles[0] == "历史会话检索", titles)
        check("UI title mcp", titles[1] == "my_server 的 find file 工具", titles)
    finally:
        registry.unregister("mcp_my_server_find_file")
    payload = json.loads(_build_voice_tool_results([
        {"name": "web_search", "tool_call_id": "c3", "content": "{\"results\": []}"},
    ]))
    check("UI title web_search", payload["agent_steps"][0]["title"] == "联网查询")
    check("empty results -> payload still valid", payload["search_failed"] is False)

    print(f"\nRESULT: {passed} passed, {failed} failed")
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write(f"voice_tool_access: {passed} passed, {failed} failed\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write("voice_tool_access: CRASH\n" + traceback.format_exc())
        raise
