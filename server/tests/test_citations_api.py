"""citations API 支撑逻辑 + assistant.citation_style 校验（P1 贯通）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.citations import _entries_from_tool_results
from app.schemas.assistant import AssistantCreate, AssistantUpdate, AssistantResponse
import pytest


def test_entries_from_tool_results_positional_ids():
    import json
    tr = json.dumps({"results": [
        {"url": "https://a.com/x", "title": "A", "published_date": "2025-01-01"},
        {"url": "https://b.com/y", "title": "B"},
    ]})
    entries = _entries_from_tool_results(tr)
    assert [e["id"] for e in entries] == [1, 2]
    assert entries[0]["published_date"] == "2025-01-01"


def test_entries_from_tool_results_list_input():
    entries = _entries_from_tool_results([{"url": "https://a.com", "title": "T"}])
    assert entries[0]["id"] == 1


def test_citation_style_validator():
    ok = AssistantCreate(name="n", model_alias="", citation_style="apa")
    assert ok.citation_style == "apa"
    empty = AssistantCreate(name="n", model_alias="", citation_style="")
    assert empty.citation_style == ""
    with pytest.raises(Exception):
        AssistantCreate(name="n", model_alias="", citation_style="not-a-style")
    up = AssistantUpdate(citation_style="gb-t-7714-numeric")
    assert up.citation_style == "gb-t-7714-numeric"
    with pytest.raises(Exception):
        AssistantUpdate(citation_style="bogus")
    # Response 宽容序列化陈旧 id
    resp = AssistantResponse(
        id="x", user_id="u", name="n", model_alias="",
        citation_style="stale-style",
        created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00",
    )
    assert resp.citation_style == "stale-style"


def test_render_api_payload_roundtrip():
    """render 请求体 → 渲染 → 保存笔记可用结构（P1 契约面）。"""
    from app.services.citation_style_service import render_citations
    entries = _entries_from_tool_results([
        {"url": "https://example.com/r", "title": "报告", "published_date": "2025-06-01"}
    ])
    r = render_citations("结论成立 [1]。", entries, style_id="gb-t-7714-numeric", accessed=(2026, 9, 27))
    assert r.bibliography_section.startswith("\n\n---\n\n## 参考文献")
    assert "<sup>[1]</sup>" in r.content
