"""RED→GREEN (2026-10-22, A4.9 B-Important-1)：存量 `[cite:…]` 数据愈合。

backfill_citation_sanitize 的 dirty 判定原先只看 `verify().unknown`；在
`normalize_cite_markers` 落地后，合法 `[cite:10]` 规范化后是 **cited**（非
unknown）→ 事故形状（合法编号的裸 `[cite:…]`）被读成 clean 而跳过重写，
用户仍看到裸标记。修后判定必须包含「normalize 前后有差异」这一臂。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _ledger(n=16):
    from app.services.citation_ledger import build_ledger_from_tool_results
    import json

    return build_ledger_from_tool_results([{
        "name": "web_search", "error": False,
        "result": json.dumps({"results": [
            {"url": f"https://example.com/{i}", "title": f"来源{i}"}
            for i in range(1, n + 1)
        ]}, ensure_ascii=False),
    }])


def test_incident_shape_with_valid_ids_is_dirty():
    """60 事故形状：合法编号的裸 [cite:…] 也必须触发重写（修复前被判 clean）。"""
    from scripts.backfill_citation_sanitize import _dirty_surfaces

    content = "[cite:10,11,12,13,14,15,16]\n答案正文 [10]。\n[cite:10,11,12,13,14,15,16]"
    assert _dirty_surfaces(content, [], _ledger()) is True


def test_cite_marker_on_display_surface_is_dirty():
    from scripts.backfill_citation_sanitize import _dirty_surfaces

    assert _dirty_surfaces("答案 [10]。", ["过程面 [cite:2]"], _ledger()) is True


def test_already_clean_message_is_not_dirty():
    from scripts.backfill_citation_sanitize import _dirty_surfaces

    assert _dirty_surfaces("答案正文 [10] 与 [16]。", ["过程面 [3]"], _ledger()) is False


def test_unknown_id_still_dirty():
    from scripts.backfill_citation_sanitize import _dirty_surfaces

    assert _dirty_surfaces("答案 [97]。", [], _ledger()) is True


def test_book_only_sources_register_without_url():
    """re-review Minor-2：无 URL 的古籍/书籍条目也要入账（register_hits 会丢弃），
    否则纯书籍会话 ledger.size=0 被早退、永不愈合；且 book 占位 id 是 cited 而非
    unknown（[1] 不得被误判——旧版弱断言只测 normalize 臂，删掉 _ledger_for 修复
    也照样绿）。"""
    from scripts.backfill_citation_sanitize import _dirty_surfaces, _ledger_for

    tr = {"results": [{"title": "古籍甲", "author": "某人", "year": "1900"}]}
    led = _ledger_for(tr)
    assert led.size == 1
    assert _dirty_surfaces("正文 [1]。", [], led) is False
    assert _dirty_surfaces("正文 [cite:1]。", [], led) is True


def test_candidate_gate_matches_has_activity():
    """re-review 轮 4 Minor-3：_candidate 放行门禁与 _sanitize_cited_content 的
    has_activity（results | agent_steps）对齐；search_failed 单独不算活动。"""
    from scripts.backfill_citation_sanitize import _candidate

    class _M:
        content = "x"

    # results 非空 → 放行
    _M.tool_results = '{"results": [{"url": "https://e.com/1", "title": "t"}]}'
    tr, _ = _candidate(_M())
    assert tr is not None
    # results 空 + agent_steps → 放行（search-failed 回合的空台账）
    _M.tool_results = '{"results": [], "agent_steps": [{"name": "web_search"}]}'
    tr, _ = _candidate(_M())
    assert tr is not None
    # results 空 + 仅 search_failed → 不放行（与 has_activity 对齐）
    _M.tool_results = '{"results": [], "search_failed": true}'
    tr, _ = _candidate(_M())
    assert tr is None
