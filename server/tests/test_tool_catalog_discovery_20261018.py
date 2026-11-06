"""T1a: citation_render lazy fix + F8 catalog discovery (2026-10-18)."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import get_config
from app.tools.mcp_search import search_tools_handler


def test_citation_render_not_lazy_web_tool():
    # Mirrors the chat.py lazy expression; the explicit exclusion is the
    # contract (F1 regression: citation_render was swallowed by the "web"
    # wildcard and invisible on the first turn).
    from app.tools.registry import registry
    lazy = [
        n for n in registry.get_tool_names_for_toolset("web")
        if n not in ("browser", "pdf_export", "web_search", "citation_render")
    ]
    assert "citation_render" not in lazy


def test_catalog_discovery_includes_non_mcp_when_enabled():
    cfg = get_config()
    old = cfg.agent.get("tools_catalog_discovery_enabled", False)
    cfg.agent["tools_catalog_discovery_enabled"] = True
    try:
        raw = asyncio.run(search_tools_handler({
            "query": "citation reference",
            "limit": 10,
        }))
        data = json.loads(raw)
        names = [m.get("name") for m in data.get("matches", [])]
        assert "citation_render" in names
    finally:
        cfg.agent["tools_catalog_discovery_enabled"] = old


def test_catalog_discovery_default_stays_mcp_only():
    cfg = get_config()
    old = cfg.agent.get("tools_catalog_discovery_enabled", False)
    cfg.agent["tools_catalog_discovery_enabled"] = False
    try:
        raw = asyncio.run(search_tools_handler({
            "query": "citation reference",
            "limit": 10,
        }))
        data = json.loads(raw)
        names = [m.get("name") for m in data.get("matches", [])]
        assert "citation_render" not in names
    finally:
        cfg.agent["tools_catalog_discovery_enabled"] = old
