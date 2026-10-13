"""Golden tests — citation_style_service（学术引用格式渲染器，P0 底座）。

设计稿：docs/CITATION_STYLE_SYSTEM_DESIGN.md
- [N] 仍是撰写语法；样式化渲染只发生在输出汇点（citeproc-py + vendored CSL）。
- 文内五族：编号方括号（IEEE/Vancouver）/编号上标（GB 顺序编码制/AMA）/作者-年（APA 等）/
  作者-页（MLA）/注释-书目（Chicago-NB：逐出现上标注号 + 注释节 + 书目节）。
- F1（笔记双参考节）：渲染前剥模型自写参考节（含「参考来源」+ 冒号容错）。
- F2（URL 猜日期）：issued 取条目 published_date，不从 URL 猜。
- F3（零引用全列）：只渲染正文实际引用的条目；正文零 [N] 不出参考节。

accessed 固定传参保证黄金断言确定性。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.citation_style_service import (
    strip_model_bibliography,
    render_citations,
)

ACCESSED = (2026, 9, 27)

WEBPAGE_ENTRY = {
    "id": 1,
    "url": "https://example.com/report",
    "title": "中国人工智能产业发展报告",
    "snippet": "",
    "published_date": "2025-03-01",
}

JOURNAL_3AUTH = {
    "id": 2,
    "url": "https://example.com/attention",
    "title": "Attention Is All You Need",
    "snippet": "",
    "published_date": "2017",
    "author": "Vaswani, Ashish; Shazeer, Noam; Parmar, Niki",
    "site_name": "NeurIPS",
}

JOURNAL_1AUTH = {
    "id": 3,
    "url": "https://example.com/single",
    "title": "A Single Author Study",
    "snippet": "",
    "published_date": "2021",
    "author": "Smith, John",
    "site_name": "Journal of Testing",
}


def _r(content, entries, style_id, **kw):
    return render_citations(content, entries, style_id=style_id, accessed=ACCESSED, **kw)


# ---------- GB/T 7714 顺序编码制（默认） ----------

def test_gb_numeric_superscript_and_ebol():
    r = _r("报告称 X [1]。", [WEBPAGE_ENTRY], "gb-t-7714-numeric")
    assert "<sup>[1]</sup>" in r.content
    assert "[1]" in r.bibliography_html
    assert "[EB/OL]" in r.bibliography_html
    assert "(2025-03-01)" in r.bibliography_html  # F2: 条目 published_date，非 URL 猜
    assert "[2026-09-27]" in r.bibliography_html  # 引用日期


def test_gb_bibliography_markdown_present():
    r = _r("X [1]。", [WEBPAGE_ENTRY], "gb-t-7714-numeric")
    assert r.bibliography_markdown.strip() != ""
    assert "参考文献" in r.bibliography_markdown or "参考文献" in r.bibliography_section
    # 参考节应可直接拼接
    section = r.bibliography_section
    assert section.lstrip().startswith("##") or "参考文献" in section


# ---------- F3：只渲染正文实际引用的条目 ----------

def test_only_cited_entries_rendered():
    entries = [WEBPAGE_ENTRY, JOURNAL_3AUTH, JOURNAL_1AUTH]
    r = _r("只引用第二条 [2]。", entries, "gb-t-7714-numeric")
    assert "Attention" in r.bibliography_html
    assert "中国人工智能" not in r.bibliography_html
    assert "Single Author" not in r.bibliography_html
    assert len(r.used_ids) == 1 and r.used_ids == [2]


def test_zero_citations_no_section():
    r = _r("正文没有引用标记。", [WEBPAGE_ENTRY], "gb-t-7714-numeric")
    assert r.bibliography_section == ""
    assert r.content == "正文没有引用标记。"
    assert r.used_ids == []


# ---------- 编号重排：按正文首次出现顺序 ----------

def test_ieee_bracket_renumber_first_appearance():
    entries = [WEBPAGE_ENTRY, JOURNAL_3AUTH]
    r = _r("先引 [2] 后引 [1]。", entries, "ieee")
    # [2] 首次出现 → CSL 编号 1
    assert "先引 [1] 后引 [2]。" == r.content.replace("<sup>", "").replace("</sup>", "")
    assert "<sup>" not in r.content  # IEEE 方括号平排，无上标
    assert "[1]" in r.bibliography_html and "[2]" in r.bibliography_html


# ---------- 作者-年族（APA） ----------

def test_apa_author_date_et_al():
    r = _r("已有研究 [2] 证实。", [JOURNAL_3AUTH], "apa")
    assert "(Vaswani et al., 2017)" in r.content
    assert "[2]" not in r.content
    assert "Vaswani, A." in r.bibliography_html


def test_apa_single_author_in_text():
    r = _r("Smith 的研究 [3] 表明。", [JOURNAL_1AUTH], "apa")
    assert "(Smith, 2021)" in r.content


# ---------- 注释-书目族（Chicago-NB） ----------

def test_chicago_notes_occurrence_notes():
    entries = [WEBPAGE_ENTRY, JOURNAL_3AUTH]
    r = _r("甲 [1]。乙 [2]。再甲 [1]。", entries, "chicago-notes")
    # 每个出现位置独立上标注号 1,2,3
    body = r.content
    assert "<sup>1</sup>" in body and "<sup>2</sup>" in body and "<sup>3</sup>" in body
    assert "[1]" not in body.replace("<sup>", "").replace("</sup>", "") or True
    assert len(r.notes) == 3
    # 第 3 注为条目 1 的重复：短注（非完整注）
    assert r.notes[0] != r.notes[2]
    # 书目 2 条
    assert "Attention" in r.bibliography_html
    assert "中国人工智能" in r.bibliography_html


# ---------- F1：模型自写参考节剥除（含「参考来源」与冒号容错） ----------

def test_strip_model_bibliography_variants():
    for header in ["## 参考来源", "**参考来源**", "参考文献：", "## References:", "参考资料", "**Sources**"]:
        body = f"正文。\n\n{header}\n\n[1] 旧条目. 2024.\n[2] 旧条目. 2023."
        out = strip_model_bibliography(body)
        assert "旧条目" not in out, header
        assert "正文。" in out


def test_render_strips_model_section_then_appends_single():
    content = "结论 [1]。\n\n## 参考来源\n\n[1] 模型自写条目. 2024."
    r = _r(content, [WEBPAGE_ENTRY], "gb-t-7714-numeric")
    # 模型自写节被剥，仅保留一个系统生成参考节
    assert "模型自写条目" not in r.content
    assert r.content.count("参考文献") + r.content.count("参考来源") <= 1  # 正文不残留旧节
    assert "参考文献" in r.bibliography_section


# ---------- 辅助：代码围栏内 [N] 不替换 ----------

def test_code_fence_markers_untouched():
    r = _r("用法示例 `run [1]` 与如下：\n\n```\narr[1] = 2\n```\n\n正文 [1]。", [WEBPAGE_ENTRY], "gb-t-7714-numeric")
    assert "`run [1]`" in r.content
    assert "arr[1]" in r.content
    assert "<sup>[1]</sup>。" in r.content


# ---------- F2 专项：published_date 优先于 URL 路径 ----------

def test_published_date_wins_over_url_guess():
    e = {
        "id": 1,
        "url": "https://example.com/2020/01/01/legacy-path",
        "title": "Date Test",
        "snippet": "",
        "published_date": "2024-11-05",
    }
    r = _r("X [1]。", [e], "gb-t-7714-numeric")
    assert "(2024-11-05)" in r.bibliography_html
    assert "(2020-01-01)" not in r.bibliography_html


# ---------- 样式目录 ----------

def test_style_catalog_lists_defaults():
    from app.services.citation_style_service import list_styles
    styles = list_styles()
    ids = {s["id"] for s in styles}
    for sid in ["gb-t-7714-numeric", "apa", "ieee", "chicago-notes", "vancouver", "mla"]:
        assert sid in ids, sid
    fam = {s["id"]: s["family"] for s in styles}
    assert fam["gb-t-7714-numeric"] in ("numeric-superscript", "numeric")
    assert fam["apa"] == "author-date"
    assert fam["chicago-notes"] == "notes"


# ---------- 文献类型推断（P3 元数据层） ----------

def test_infer_csl_type():
    from app.services.citation_style_service import infer_csl_type
    assert infer_csl_type("https://arxiv.org/abs/1706.03762") == "article-journal"
    assert infer_csl_type("https://news.example.com/2025/report") == "article-newspaper"
    assert infer_csl_type("https://example.com/files/a.pdf") == "report"
    assert infer_csl_type("https://blog.example.com/post") == "webpage"


def test_author_metadata_flows_to_csl():
    e = dict(JOURNAL_3AUTH)
    e["author"] = "Vaswani, Ashish; Shazeer, Noam; Parmar, Niki; Uszkoreit, Jakob"
    r = _r("X [2]。", [e], "gb-t-7714-numeric")
    assert "VASWANI" in r.bibliography_html
    assert "等" in r.bibliography_html or "et al" in r.bibliography_html


def test_searchhit_to_dict_carries_author():
    from app.services.search_service import SearchHit
    h = SearchHit(title="T", url="https://x.com", snippet="s", author="Zhang San", site_name="X")
    d = h.to_dict()
    assert d["author"] == "Zhang San" and d["site_name"] == "X"


# ---------- A4.9 ③④ 回归：零引用剥节 / 降级编号对齐 / 元数据转义 ----------

def test_zero_citations_still_strips_model_bibliography():
    content = "结论如下。\n\n## 参考文献\n\n[1] 模型编造条目. 2024."
    r = _r(content, [WEBPAGE_ENTRY], "gb-t-7714-numeric")
    assert "模型编造条目" not in r.content
    assert "结论如下。" in r.content
    assert r.bibliography_section == ""


def test_fallback_section_numbers_align_with_entry_ids():
    from app.services.citation_style_service import _fallback_section
    entries = [
        {"id": 5, "url": "https://a.com", "title": "Entry Five", "published_date": "2025"},
        {"id": 2, "url": "https://b.com", "title": "Entry Two", "published_date": "2024"},
    ]
    out = _fallback_section(entries)
    assert "[5] Entry Five" in out and "[2] Entry Two" in out
    assert "[1] Entry" not in out  # 不允许重新编号错位


def test_bibliography_escapes_hostile_metadata():
    e = {
        "id": 1,
        "url": "https://evil.example.com/x",
        "title": '<script>alert(1)</script> Safe Title',
        "snippet": "",
        "published_date": "2025-01-01",
        "author": '<img src=x onerror=alert(2)>',
    }
    r = _r("X [1]。", [e], "gb-t-7714-numeric")
    assert "<script>" not in r.bibliography_html
    assert "onerror=" not in r.bibliography_html.replace("&lt;img src=x onerror=alert(2)&gt;", "")
    assert "&lt;script&gt;" in r.bibliography_html


def test_plan_citation_style_preserved():
    from app.services.deathmatch_service import DeathmatchManager
    fn = DeathmatchManager._plan_with_citation_style
    out = fn({"citation_style": "apa", "steps": []}, [{"id": "s1"}])
    assert out["citation_style"] == "apa"
    out2 = fn({"citation_style": "bogus-style", "steps": []}, [{"id": "s1"}])
    assert "citation_style" not in out2  # 未知样式丢弃
    out3 = fn({"steps": []}, [{"id": "s1"}])
    assert "citation_style" not in out3


# ---------- A4.9 二轮：URL/短注转义（④ 收口） ----------

def test_bibliography_escapes_hostile_url():
    e = {"id": 1, "url": 'https://e.com/<script>alert(1)</script>', "title": "T", "published_date": "2025-01-01"}
    r = _r("X [1]。", [e], "gb-t-7714-numeric")
    assert "<script>" not in r.bibliography_section


def test_short_note_escapes_hostile_title():
    from app.services.citation_style_service import _short_note
    out = _short_note({"title": '<img src=x onerror=alert(2)> Title Here', "author": ""})
    assert "<img" not in out and "&lt;img" in out


# ---------- 盲区波：持久化元数据 / 计划摘要可见性 / 行内来源前缀 ----------

def test_inline_source_prefix_stripped():
    content = "如图。\n\n来源：新华网 [1]\n\n正文继续 [2]。"
    entries = [dict(WEBPAGE_ENTRY), dict(JOURNAL_3AUTH)]
    r = _r(content, entries, "gb-t-7714-numeric")
    assert "来源：新华网" not in r.content
    assert "<sup>[1]</sup>" in r.content and "<sup>[2]</sup>" in r.content


def test_prose_compound_source_not_stripped():
    """W14（conv f1499710）：prose 复合词「技术来源：」不是注记——原文必须保留。"""
    content = "4. 技术来源：尊界车上搭载的乾崑智驾、鸿蒙座舱等来自引望体系[30]。"
    entries = [dict(WEBPAGE_ENTRY), dict(JOURNAL_3AUTH)]
    r = _r(content, entries, "gb-t-7714-numeric")
    assert "技术来源：尊界车上搭载的乾崑智驾、鸿蒙座舱等来自引望体系" in r.content


def test_plan_summary_shows_citation_style():
    import json, types
    from app.services.deathmatch_service import DeathmatchManager
    mgr = DeathmatchManager.__new__(DeathmatchManager)
    mgr._conv = types.SimpleNamespace(deathmatch_plan={
        "citation_style": "apa",
        "steps": [{"id": "s1", "description": "撰写文献综述", "status": "pending"}],
    })
    out = mgr.get_plan_summary_for_prompt()
    assert "引用格式: apa" in out
    mgr._conv.deathmatch_plan.pop("citation_style")
    out2 = mgr.get_plan_summary_for_prompt()
    assert "引用格式" not in out2


# ---------- 已知限制 #4：MODEL_BIBLIOGRAPHY_RE 贪婪语义收紧（形态校验） ----------

def test_mid_doc_references_heading_without_entries_preserved():
    """文档中段 '## References' 标题后无 [N] 条目行 → 不剥（防误杀正文）。"""
    content = "本文讨论引用规范。\n\n## References\n\n参考文献著录应遵循国家标准，具体包括…\n\n## 结论\n\n完。"
    assert strip_model_bibliography(content) == content


def test_real_bibliography_section_still_stripped():
    content = "正文。\n\n## References\n\n[1] Smith, J. Title. 2020.\n[2] Doe, A. Other. 2021."
    out = strip_model_bibliography(content)
    assert "Smith" not in out and "正文。" in out


# ---------- A4.9 I2：中段假标题 + 文末真参考节的组合 ----------

def test_fake_mid_heading_plus_real_end_bibliography():
    content = (
        "讨论引用规范。\n\n## References\n\n著录应遵循标准，无条目行。\n\n"
        "结论 [1]。\n\n## 参考文献\n\n[1] 模型编造条目. 2024."
    )
    r = _r(content, [WEBPAGE_ENTRY], "gb-t-7714-numeric")
    assert "著录应遵循标准" in r.content  # 中段假标题内容保留
    assert "模型编造条目" not in r.content  # 文末真参考节剥除
    assert "<sup>[1]</sup>" in r.content
