"""M2 tests — browser 来源入账定号 + 全链路编号对齐。

plan_citation_full_discipline_ref_20261011 M2：
- browser/grep 古籍页入账时轻量抽取书目项（extract_biblio_hints）。
- 入账即定号：digest 信封 `## 来源` 行带 `[N]`；JSON pages 带 citation_id。
- live（AgentLoop._apply_citation_ledger）与 persistence
  （chat._transform_tool_loop_results）按同一调用顺序注册 → 编号逐位对齐。

Run: cd backend && python -m pytest tests/test_browser_citation_numbering.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.chat import _transform_tool_loop_results
from app.services.agent_loop import AgentLoop, AgentLoopState, ToolCallResult
from app.services.citation_sources import (
    extract_biblio_hints,
    extract_browser_sources,
    extract_host_hints,
)

DIGEST_ENVELOPE = (
    "<tool-digest>\n"
    "【工具】browser\n"
    "【目标】https://shidianguji.com/book/SDZ123\n"
    "【原始大小】12,345 字符\n"
    "【全文存档】/tmp/archive.txt\n"
    "【用法】如需细节请读取全文存档。\n"
    "<digest>\n"
    "## 来源\n"
    "`https://shidianguji.com/book/SDZ123` (摄山志)\n"
    "\n"
    "## 核心要点\n"
    "1. 摄山志卷四记载乾隆南巡。\n"
    "</digest>\n"
    "</tool-digest>"
)


def _search_result(urls):
    return json.dumps({
        "count": len(urls),
        "results": [{"url": u, "title": f"标题-{i}", "snippet": f"摘要-{i}"} for i, u in enumerate(urls)],
    }, ensure_ascii=False)


def _browser_json(pages):
    return json.dumps({"pages": pages}, ensure_ascii=False)


# ---------- 书目抽取 ----------

def test_extract_biblio_hints_canonical_forms():
    text = "据著录：王夫之.宋论[M].刻本.金陵:湘乡曾国荃,1865。馆藏善本。"
    hints = extract_biblio_hints(text)
    assert hints.get("author") == "王夫之"
    assert hints.get("title") == "宋论"
    assert hints.get("type") == "book"
    assert hints.get("edition") == "刻本"
    assert hints.get("publisher_place") == "金陵"
    assert hints.get("publisher") == "湘乡曾国荃"
    assert hints.get("year") == "1865"


def test_extract_biblio_hints_no_match_returns_empty():
    assert extract_biblio_hints("这段文字没有任何书目引用格式。") == {}


def test_extract_host_hints():
    h = extract_host_hints("https://shidianguji.com/book/SDZ123")
    assert h.get("source_type") == "book"
    assert h.get("genre") == "古籍"


def test_extract_browser_sources_json_shape_with_biblio():
    raw = _browser_json([
        {"url": "https://shidianguji.com/book/x", "title": "摄山志", "text": "陈毅.摄山志:八卷[M].刻本.金陵:汪志伊,1790"},
        {"url": "https://bad.example.com", "title": "bad", "text": "x", "error": "timeout"},
    ])
    sources = extract_browser_sources(raw)
    assert len(sources) == 1
    url, title, snippet, biblio = sources[0]
    assert url == "https://shidianguji.com/book/x"
    assert biblio.get("genre") == "古籍"
    assert biblio.get("edition") == "刻本"


def test_extract_browser_sources_envelope_shape():
    sources = extract_browser_sources(DIGEST_ENVELOPE)
    urls = [s[0] for s in sources]
    assert "https://shidianguji.com/book/SDZ123" in urls


# ---------- live ↔ persistence 编号对齐 ----------

def test_browser_live_and_persist_numbering_aligned():
    seq = [
        ("web_search", _search_result(["https://a.com/1", "https://b.com/2"])),
        ("browser", _browser_json([{"url": "https://c.com/page", "title": "C", "text": "text"}])),
        ("web_search", _search_result(["https://d.com/3"])),
    ]
    # live 路径
    state = AgentLoopState(messages=[{"role": "user", "content": "x"}])
    live_results = [
        ToolCallResult(call_id=f"c{i}", name=name, arguments={}, result=payload, error=False)
        for i, (name, payload) in enumerate(seq)
    ]
    AgentLoop._apply_citation_ledger(state, live_results)
    live_ids = {e.get("url"): e["id"] for e in state.citation_ledger.entries}

    # persistence 路径
    accumulated = [
        {"name": name, "call_id": f"c{i}", "result": payload, "error": False}
        for i, (name, payload) in enumerate(seq)
    ]
    out = _transform_tool_loop_results(accumulated)
    payload = json.loads(out)
    persist_ids = {r["url"]: r["id"] for r in payload["results"]}

    assert live_ids == persist_ids, f"live={live_ids} persist={persist_ids}"
    # browser 页必须进入 results[]（可被引用、可出卡片）
    assert "https://c.com/page" in persist_ids


def test_browser_only_turn_persists_sources():
    accumulated = [
        {"name": "browser", "call_id": "c0",
         "result": _browser_json([{"url": "https://c.com/page", "title": "C", "text": "t"}]),
         "error": False},
    ]
    out = _transform_tool_loop_results(accumulated)
    payload = json.loads(out)
    assert payload["results"][0]["url"] == "https://c.com/page"
    assert payload["results"][0]["id"] == 1


# ---------- 入账定号：尾注烙 digest 头 / JSON citation_id ----------

def test_digest_envelope_sources_stamped_with_ids():
    state = AgentLoopState(messages=[{"role": "user", "content": "x"}])
    r = ToolCallResult(call_id="c1", name="browser", arguments={}, result=DIGEST_ENVELOPE, error=False)
    AgentLoop._apply_citation_ledger(state, [r])
    assert state.citation_ledger.size == 1
    AgentLoop._stamp_browser_citation_ids(state, [r])
    assert "[1] `https://shidianguji.com/book/SDZ123`" in r.result
    # 幂等：二次执行不重复
    AgentLoop._stamp_browser_citation_ids(state, [r])
    assert r.result.count("[1] `https://shidianguji.com/book/SDZ123`") == 1


def test_browser_json_pages_get_citation_id():
    state = AgentLoopState(messages=[{"role": "user", "content": "x"}])
    raw = _browser_json([{"url": "https://c.com/page", "title": "C", "text": "t"}])
    r = ToolCallResult(call_id="c1", name="browser", arguments={}, result=raw, error=False)
    AgentLoop._apply_citation_ledger(state, [r])
    AgentLoop._stamp_browser_citation_ids(state, [r])
    data = json.loads(r.result)
    assert data["pages"][0].get("citation_id") == 1


# ---------- A4.9 复审回归（2026-10-11）：Critical/Important 修复 ----------

def test_biblio_bearing_page_alignment_both_paths():
    """A4.9 Critical-1：古籍引文串页（extract_biblio_hints 产出 title/type）
    必须能双路径注册且编号对齐（旧实现 register 撞名 TypeError 被吞）。"""
    page_text = "陈毅.摄山志:八卷[M].刻本.金陵:汪志伊,1790"
    seq = [
        ("browser", _browser_json([{"url": "https://shidianguji.com/book/x", "title": "摄山志", "text": page_text}])),
        ("web_search", _search_result(["https://a.com/1"])),
    ]
    state = AgentLoopState(messages=[{"role": "user", "content": "x"}])
    live_results = [
        ToolCallResult(call_id=f"c{i}", name=name, arguments={}, result=payload, error=False)
        for i, (name, payload) in enumerate(seq)
    ]
    AgentLoop._apply_citation_ledger(state, live_results)
    live_ids = {e.get("url"): e["id"] for e in state.citation_ledger.entries}
    assert "https://shidianguji.com/book/x" in live_ids, "古籍页必须入 live 台账（不得被吞）"
    entry = state.citation_ledger.entries[0]
    assert entry.get("edition") == "刻本"
    # C5（遗留清零）：live 与 persist 标题优先级一致（biblio 书目题名 > 页面 title）
    assert entry.get("title") == "摄山志:八卷", entry.get("title")

    accumulated = [
        {"name": name, "call_id": f"c{i}", "result": payload, "error": False}
        for i, (name, payload) in enumerate(seq)
    ]
    out = json.loads(_transform_tool_loop_results(accumulated))
    persist_ids = {r["url"]: r["id"] for r in out["results"]}
    assert live_ids == persist_ids, f"live={live_ids} persist={persist_ids}"
    assert out["results"][0]["title"] == "摄山志:八卷", out["results"][0]["title"]


def test_stamp_prefix_url_not_hijacked():
    """A4.9 Important-2：前缀 URL 不得劫持更长 URL 的编号（token 整匹配）。"""
    envelope = (
        "<tool-digest>\n【工具】browser\n【目标】https://a.com/page2\n"
        "【原始大小】9,999 字符\n【全文存档】/tmp/x.txt\n【用法】…\n<digest>\n"
        "## 来源\n`https://a.com/page2` (长页)\n`https://a.com/page` (短页)\n"
        "</digest>\n</tool-digest>"
    )
    state = AgentLoopState(messages=[{"role": "user", "content": "x"}])
    r = ToolCallResult(call_id="c1", name="browser", arguments={}, result=envelope, error=False)
    # 注册顺序：先长后短（模拟页面 order）
    state.citation_ledger = led = __import__(
        "app.services.citation_ledger", fromlist=["CitationLedger"]
    ).CitationLedger()
    led.register("https://a.com/page2", title="长页")
    led.register("https://a.com/page", title="短页")
    AgentLoop._stamp_browser_citation_ids(state, [r])
    text = r.result
    assert "[1] `https://a.com/page2`" in text
    assert "[2] `https://a.com/page`" in text
    assert "[2] `https://a.com/page2`" not in text, "短页编号不得劫持长页"


def test_stamp_fragment_url_normalized_lookup():
    """A4.9 B4/⑪：带 fragment/尾斜杠的 URL 也要能按规范化命中编号。"""
    envelope = (
        "<tool-digest>\n【工具】browser\n【目标】https://b.com/x/#sec\n"
        "【原始大小】9,999 字符\n【全文存档】/tmp/y.txt\n【用法】…\n<digest>\n"
        "## 来源\n`https://b.com/x/#sec` (页)\n</digest>\n</tool-digest>"
    )
    state = AgentLoopState(messages=[{"role": "user", "content": "x"}])
    r = ToolCallResult(call_id="c1", name="browser", arguments={}, result=envelope, error=False)
    AgentLoop._apply_citation_ledger(state, [r])
    assert state.citation_ledger.size == 1
    AgentLoop._stamp_browser_citation_ids(state, [r])
    assert "[1] `https://b.com/x/#sec`" in r.result


def test_persisted_output_recovery_alignment(tmp_path):
    """A4.9 Important-3：大结果被 budget 包装（<persisted-output>）时，
    持久化按指针恢复同源 JSON → 编号仍对齐。"""
    pages = {"pages": [{"url": "https://c.com/big", "title": "B", "text": "t" * 200}]}
    fpath = tmp_path / "big.txt"
    fpath.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    wrapped = (
        "<persisted-output>\nThis tool result was too large (160.0 KB).\n"
        f"Full output saved to: {fpath}\n"
        "Use the file reading capabilities to access specific sections if needed.\n\n"
        "Preview (first 500 chars):\n{\"pages\": …"
    )
    state = AgentLoopState(messages=[{"role": "user", "content": "x"}])
    # live 注册发生在 budget 包装前（原始 JSON）——模拟：
    raw = json.dumps(pages, ensure_ascii=False)
    live_r = ToolCallResult(call_id="c1", name="browser", arguments={}, result=raw, error=False)
    AgentLoop._apply_citation_ledger(state, [live_r])
    live_ids = {e["url"]: e["id"] for e in state.citation_ledger.entries}

    accumulated = [{"name": "browser", "call_id": "c1", "result": wrapped, "error": False}]
    out = json.loads(_transform_tool_loop_results(accumulated))
    persist_ids = {r["url"]: r["id"] for r in out["results"]}
    assert persist_ids == live_ids, f"persist={persist_ids} live={live_ids}"


def test_sanitize_keeps_url_less_entries_valid():
    """A4.9 Important-4：results[] 含无 URL 条目时 sanitize 台账仍按位对齐，
    合法 [1]（古籍）不被误判剥离。[N] 判断走 LLM 消歧——mock 之。"""
    import asyncio as _asyncio
    import app.services.agentic_judge as aj
    from app.api.chat import _sanitize_cited_content

    content = "《摄山志》卷四有记载 [1]，另见报告 [2]。"
    tr = json.dumps({"results": [
        {"id": 1, "url": "", "title": "摄山志:八卷", "author": "陈毅", "year": "1790", "snippet": "卷四"},
        {"id": 2, "url": "https://example.com/r", "title": "报告", "snippet": "x"},
    ]}, ensure_ascii=False)
    orig = aj.judge_json

    async def _fake_judge(system_prompt, user_prompt, **kwargs):
        return {"citation_ids": []}

    aj.judge_json = _fake_judge
    try:
        new_content, _ = _asyncio.run(_sanitize_cited_content(content, tr))
    finally:
        aj.judge_json = orig
    assert "[1]" in new_content and "[2]" in new_content, new_content


def test_replay_denumbers_browser_sources():
    """A4.9 B1：历史轮 browser 结果的 [N] 尾注/ citation_id 在回放前剥离。"""
    from app.services.tool_history import _denumber_browser_sources

    envelope = "【目标】[3] https://a.com/x\n## 来源\n[3] `https://a.com/x` (页)"
    out = _denumber_browser_sources(envelope)
    assert "[3]" not in out
    assert "https://a.com/x" in out
    page_json = json.dumps({"pages": [{"url": "https://a.com/x", "citation_id": 3}]})
    out2 = _denumber_browser_sources(page_json)
    assert "citation_id" not in out2
    json.loads(out2)  # A-F2：解析优先——回放文本必须是合法 JSON（不留尾逗号）
    # C6（遗留清零）：正文中段的页面原文「[7] https://…」不得误剥（行锚）
    prose = "## 正文\nEvidence: [7] https://example.com/evidence 为原文编号"
    assert "Evidence: [7] https://example.com/evidence" in _denumber_browser_sources(prose)
    # 行首编号形（我们烙的形态）仍剥
    stamped = "## 来源\n[7] `https://example.com/evidence` (页)"
    assert "[7]" not in _denumber_browser_sources(stamped)


def test_web_search_persisted_output_recovery(tmp_path):
    """A4.9 r2 Important：budget 包装的大 web_search 结果也必须按指针恢复
    （旧实现只对 <tool-digest> 恢复——大搜索静默丢证据 + 编号断链）。"""
    hits = {"results": [
        {"url": f"https://w.com/{i}", "title": f"T{i}", "snippet": "s"} for i in range(3)
    ]}
    fpath = tmp_path / "search.txt"
    fpath.write_text(json.dumps(hits, ensure_ascii=False), encoding="utf-8")
    wrapped = (
        "<persisted-output>\nThis tool result was too large (123.0 KB).\n"
        f"Full output saved to: {fpath}\n"
        "Use the file reading capabilities to access specific sections if needed.\n\n"
        'Preview (first 500 chars):\n{"count": 3, …'
    )
    accumulated = [{"name": "web_search", "call_id": "c1", "result": wrapped, "error": False}]
    out = json.loads(_transform_tool_loop_results(accumulated))
    assert len(out["results"]) == 3
    assert out["results"][0]["url"] == "https://w.com/0"


def test_stamp_skips_markdown_links():
    """A4.9 r2 Minor：markdown 链接 [text](url) 内的 URL 不烙编号（破坏链接）。"""
    envelope = (
        "<tool-digest>\n【工具】browser\n【目标】https://c.com/x\n"
        "【原始大小】9,999 字符\n【全文存档】/tmp/z.txt\n【用法】…\n<digest>\n"
        "## 来源\n详见 [报告](https://c.com/x)，正文另述。\n"
        "</digest>\n</tool-digest>"
    )
    state = AgentLoopState(messages=[{"role": "user", "content": "x"}])
    r = ToolCallResult(call_id="c1", name="browser", arguments={}, result=envelope, error=False)
    AgentLoop._apply_citation_ledger(state, [r])
    AgentLoop._stamp_browser_citation_ids(state, [r])
    assert "[1] https://c.com/x" in r.result, "目标行（行锚）仍烙印"
    assert "[报告](https://c.com/x)" in r.result, "markdown 链接不得被破坏"


def test_stamp_line_anchor_inverse_with_denumber():
    """A4.9 清理轮 A-F1：盖章与回放去号**互逆**——行中正文出现的 URL 不烙，
    回放后不留任何陈旧 [N]（含同一行第二处、长前缀处）。"""
    from app.services.tool_history import _denumber_browser_sources

    envelope = (
        "<tool-digest>\n【工具】browser\n【目标】https://a.com/x https://b.com/y\n"
        "【原始大小】9,999 字符\n【全文存档】/tmp/z.txt\n【用法】…\n<digest>\n"
        "## 来源\n`https://a.com/x` (甲)\n`https://b.com/y` (乙)\n"
        "## 核心要点\n正文提到链接：https://a.com/x（行中前缀>6，不烙）。\n"
        "</digest>\n</tool-digest>"
    )
    state = AgentLoopState(messages=[{"role": "user", "content": "x"}])
    r = ToolCallResult(call_id="c1", name="browser", arguments={}, result=envelope, error=False)
    AgentLoop._apply_citation_ledger(state, [r])
    AgentLoop._stamp_browser_citation_ids(state, [r])
    persisted = r.result
    # 来源行两处都烙
    assert "[1] `https://a.com/x`" in persisted
    assert "[2] `https://b.com/y`" in persisted
    # 行中（行首前缀 >6）正文出现不烙
    assert "正文提到链接：https://a.com/x" in persisted
    assert "正文提到链接：[1]" not in persisted
    # 去号（回放）后：不留任何我们烙的 [N]（互逆——无剥不净）
    replayed = _denumber_browser_sources(persisted)
    assert "[1]" not in replayed and "[2]" not in replayed
    assert "https://a.com/x" in replayed and "https://b.com/y" in replayed


def test_sanitize_snippet_only_entry_keeps_positions():
    """A4.9 r2 Minor：snippet-only（无 url/title/author/year）条目也占位注册，
    后续编号不位移。"""
    import asyncio as _asyncio
    import app.services.agentic_judge as aj
    from app.api.chat import _sanitize_cited_content

    content = "第一个来源 [1]，第二个 [2]。"
    tr = json.dumps({"results": [
        {"id": 1, "url": "", "title": "", "snippet": "只有摘要"},
        {"id": 2, "url": "https://x.com/2", "snippet": "y"},
    ]}, ensure_ascii=False)
    orig = aj.judge_json

    async def _fake_judge(system_prompt, user_prompt, **kwargs):
        return {"citation_ids": []}

    aj.judge_json = _fake_judge
    try:
        new_content, _ = _asyncio.run(_sanitize_cited_content(content, tr))
    finally:
        aj.judge_json = orig
    assert "[1]" in new_content and "[2]" in new_content, new_content
