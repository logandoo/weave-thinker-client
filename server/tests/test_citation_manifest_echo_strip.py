"""P0 R1: trailing citation-manifest echo must be stripped before persistence.

The model occasionally copies the system citation ledger into the answer tail
(`[1] Title` per line).  Those lines are not prose citations: the frontend
already renders source cards from the ``citations`` payload.  Strip only a
TRAILING block whose ids and titles match the turn ledger, never inline [N].
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.chat_citations import _sanitize_cited_content, strip_stop_citation_echo
from app.services.citation_ledger import CitationLedger


def _ledger() -> CitationLedger:
    ledger = CitationLedger()
    ledger.register("https://example.com/alpha", title="Alpha Study Results")
    ledger.register("https://example.com/beta", title="Beta Market Report")
    return ledger


def _ledger8() -> CitationLedger:
    ledger = CitationLedger()
    for i in range(1, 9):
        title = "Alpha Study Results" if i == 1 else "Beta Market Report" if i == 2 else f"Source Title {i}"
        ledger.register(f"https://example.com/{i}", title=title)
    return ledger


def _tail8(ledger: CitationLedger, *, heads: bool = False) -> str:
    lines = []
    for e in ledger.entries:
        head = ledger._entry_manifest_head(e) if heads else e["title"]
        lines.append(f"[{e['id']}] {head}")
    return "\n".join(lines)


def test_manifest_author_year_suffix_is_stripped():
    ledger = _ledger8()
    ledger.entries[0].update({"author": "Alice", "year": "2026", "volume": "12", "page": "33"})
    text = "正文[1]。\n\n" + _tail8(ledger, heads=True) + "\n"
    assert ledger.strip_trailing_manifest_echo(text) == "正文[1]。"


def test_numbered_manifest_tail_is_stripped_and_inline_citation_kept():
    ledger = _ledger8()
    text = "结论：Alpha 增长 12%[1]。\n\n" + _tail8(ledger) + "\n"
    out = ledger.strip_trailing_manifest_echo(text)
    assert out == "结论：Alpha 增长 12%[1]。"
    assert "[1] Alpha Study Results" not in out


def test_plain_title_tail_is_preserved_because_it_can_be_the_answer():
    # Review R1/R2 Critical: a user may ask to list these source titles; a
    # plain-title block has no [N] id and must never be deleted.
    text = "结论摘要。\n\nAlpha Study Results\nBeta Market Report\n"
    assert _ledger().strip_trailing_manifest_echo(text) == text


def test_title_plus_commentary_is_not_a_manifest_echo():
    # Review R1 Critical-1: prefix matching would delete this substantive prose.
    text = (
        "三条发现：\n\n"
        "[1] Alpha Study Results 显示同比增长 12%\n"
        "[2] Beta Market Report 显示下滑 3%\n"
    )
    assert _ledger().strip_trailing_manifest_echo(text) == text


def test_21_line_incident_shape_is_stripped_without_blank_separator():
    entries = [f"Source Title {i}" for i in range(1, 22)]
    ledger = CitationLedger()
    for i, title in enumerate(entries, 1):
        ledger.register(f"https://example.com/{i}", title=title)
    tail = "\n".join(f"[{i}] {t}" for i, t in enumerate(entries, 1))
    text = "正文[1]。\n" + tail + "\n"
    assert ledger.strip_trailing_manifest_echo(text) == "正文[1]。"


def test_short_requested_source_list_without_blank_is_preserved():
    text = "列出这两个来源：\n[1] Alpha Study Results\n[2] Beta Market Report\n"
    assert _ledger().strip_trailing_manifest_echo(text) == text


def test_unclosed_code_fence_tail_is_preserved():
    text = "示例：\n\n```\n[1] Alpha Study Results\n[2] Beta Market Report\n"
    assert _ledger().strip_trailing_manifest_echo(text) == text


def test_short_source_list_with_heading_is_preserved():
    text = "以下是我引用的来源：\n参考来源：\n[1] Alpha Study Results\n[2] Beta Market Report\n"
    assert _ledger().strip_trailing_manifest_echo(text) == text


def test_manifest_scale_heading_is_removed_with_the_echo_block():
    entries = [f"Source Title {i}" for i in range(1, 10)]
    ledger = CitationLedger()
    for i, title in enumerate(entries, 1):
        ledger.register(f"https://example.com/{i}", title=title)
    tail = "\n".join(f"[{i}] {t}" for i, t in enumerate(entries, 1))
    text = "结论。\n参考资料：\n" + tail + "\n"
    assert ledger.strip_trailing_manifest_echo(text) == "结论。"


def test_enumeration_and_nonmatching_tail_are_preserved():
    assert _ledger().strip_trailing_manifest_echo("三个要点：\n[3]个要点\n[4]个要点\n") == "三个要点：\n[3]个要点\n[4]个要点\n"
    # A fabricated title under a real id must not be treated as a ledger echo.
    text = "正文[1]。\n\n[1] Fabricated Internal Title\n[2] Beta Market Report\n"
    assert _ledger().strip_trailing_manifest_echo(text) == text


def test_single_trailing_citation_line_is_not_a_manifest_echo():
    text = "正文。\n\n[1] Alpha Study Results\n"
    assert _ledger().strip_trailing_manifest_echo(text) == text


def test_sanitize_cited_content_cleans_content_segments_and_display_sequence():
    ledger = _ledger8()
    content = "正文[1]。\n\n" + _tail8(ledger) + "\n"
    tr = {
        "results": [
            {"id": e["id"], "url": e["url"], "title": e["title"]} for e in ledger.entries
        ],
        "citations": {"entries": [{"id": e["id"]} for e in ledger.entries]},
        "content_segments": [content],
        "display_sequence": [
            {"type": "text", "content": content},
            {"type": "reasoning_step", "content": "keep me"},
        ],
    }
    new_content, new_tr = asyncio.run(_sanitize_cited_content(content, json.dumps(tr, ensure_ascii=False)))
    assert new_content == "正文[1]。"
    obj = json.loads(new_tr)
    assert obj["content_segments"] == ["正文[1]。"]
    assert obj["display_sequence"][0]["content"] == "正文[1]。"
    assert obj["display_sequence"][1]["content"] == "keep me"
    assert obj["citations"] == tr["citations"]


def test_no_search_activity_leaves_text_untouched():
    text = "正文。\n\nAlpha Study Results\nBeta Market Report\n"
    new_content, _ = asyncio.run(_sanitize_cited_content(text, None))
    assert new_content == text


def test_raw_tool_result_list_is_left_untouched_without_exception():
    raw = json.dumps([{"call_id": "c1", "name": "web_search", "result": "ok"}])
    content = "正文[1]。"
    new_content, new_tr = asyncio.run(_sanitize_cited_content(content, raw))
    assert new_content == content
    assert new_tr == raw


def test_stop_path_strips_echo_by_rebuilding_ledger_from_raw_results():
    ledger = _ledger8()
    raw = [
        {"call_id": "c1", "name": "web_search", "error": False, "result": json.dumps({
            "results": [{"url": e["url"], "title": e["title"]} for e in ledger.entries]
        }, ensure_ascii=False)},
    ]
    content = "正文[1]。\n\n" + _tail8(ledger) + "\n"
    assert strip_stop_citation_echo(content, raw) == "正文[1]。"
