"""T1/T2 golden matrix + M1/M3 data chain — 全学科引用渲染矩阵。

plan_citation_full_discipline_ref_20261011 §5：
- T1：11 样式 × ≥12 文献类型，每格断言（a）文内族形态（b）书目关键字段。
- T2：无 URL 古籍条目 11 样式均出完整规范 reference（版本/卷/出版项照录），
  不回退 URL/不产生空 URL 痕迹。
- M1：台账条目书目字段 + 无 URL 派生去重键。
- M3：`_entry_to_csl` 全映射（显式 source_type 优先于 URL 推断、URL 仅存在才写、
  title 缺失回退 container+year）。

Run: cd backend && python -m pytest tests/test_citation_biblio_matrix.py -q
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.citation_ledger import CitationLedger, ledger_key
from app.services.citation_style_service import (
    STYLE_REGISTRY,
    csl_type_for_entry,
    render_citations,
    render_entry_references,
)

ACCESSED = (2026, 10, 11)

FAMILY = {
    "gb-t-7714-numeric": "numeric-superscript",
    "gb-t-7714-author-date": "author-date",
    "gb-t-7714-note": "notes",
    "apa": "author-date",
    "mla": "author-page",
    "chicago-notes": "notes",
    "chicago-author-date": "author-date",
    "ieee": "numeric-bracket",
    "vancouver": "numeric-bracket",
    "ama": "numeric-superscript",
    "harvard": "author-date",
}

# 13 文献类型夹具 —— 全部字段按 M1 台账 schema 提供。
ENTRIES = {
    "webpage": {
        "id": 1, "url": "https://example.com/ai-report", "source_type": "web",
        "title": "中国人工智能产业发展报告", "author": "工信部",
        "published_date": "2025-03-01",
    },
    "book": {
        "id": 2, "url": "https://example.com/book", "source_type": "book",
        "title": "人工智能导论", "author": "张三; 李四", "edition": "第3版",
        "publisher_place": "北京", "publisher": "高等教育出版社", "year": "2020",
    },
    "ancient_book": {  # T2: 无 URL 古籍
        "id": 3, "url": "", "source_type": "book", "genre": "古籍",
        "title": "摄山志:八卷", "author": "陈毅", "edition": "刻本",
        "publisher_place": "金陵", "publisher": "汪志伊", "year": "1790",
        "volume": "卷四",
    },
    "gazetteer": {
        "id": 4, "url": "https://shidianguji.com/book/SDZ123", "source_type": "gazetteer",
        "genre": "方志", "title": "金陵梵刹志", "author": "葛寅亮", "edition": "刻本",
        "publisher_place": "南京", "year": "1607", "container": "识典古籍",
    },
    "article-journal": {
        "id": 5, "url": "https://example.com/attention", "source_type": "periodical",
        "type": "article-journal", "title": "Attention Is All You Need",
        "author": "Vaswani, Ashish; Shazeer, Noam", "container": "NeurIPS",
        "volume": "30", "page": "5998-6008", "year": "2017",
    },
    "article-magazine": {
        "id": 6, "url": "https://example.com/wired", "source_type": "periodical",
        "type": "article-magazine", "title": "The AI Issue", "author": "Smith, Jane",
        "container": "Wired", "year": "2024",
    },
    "article-newspaper": {
        "id": 7, "url": "https://example.com/news", "source_type": "periodical",
        "type": "article-newspaper", "title": "AI 大会召开", "author": "新华社",
        "container": "人民日报", "year": "2024",
    },
    "chapter": {
        "id": 8, "url": "https://example.com/chapter", "source_type": "book",
        "type": "chapter", "title": "引言", "author": "张三", "container": "人工智能导论",
        "publisher": "高等教育出版社", "publisher_place": "北京", "page": "1-20",
        "year": "2020",
    },
    "paper-conference": {
        "id": 9, "url": "https://example.com/acl", "source_type": "periodical",
        "type": "paper-conference", "title": "A Study of Citations",
        "author": "Lee, Kai", "container": "Proc. of ACL", "year": "2023",
    },
    "standard": {
        "id": 10, "url": "https://example.com/standard", "source_type": "web",
        "genre": "标准", "title": "信息与文献 参考文献著录规则",
        "standard_no": "GB/T 7714—2015", "year": "2015",
    },
    "patent": {
        "id": 11, "url": "https://example.com/patent", "source_type": "web",
        "genre": "专利", "title": "一种古籍数字化方法", "author": "王某",
        "standard_no": "CN114000000A", "year": "2020",
    },
    "thesis": {
        "id": 12, "url": "https://example.com/thesis", "source_type": "web",
        "genre": "学位论文", "type": "thesis", "title": "深度学习古籍识别研究",
        "author": "李四", "publisher": "清华大学", "publisher_place": "北京",
        "genre_note": "博士学位论文", "year": "2021",
    },
    "report": {
        "id": 13, "url": "https://example.com/report", "source_type": "web",
        "type": "report", "title": "古籍数字化年度报告", "author": "某机构",
        "publisher": "某机构", "standard_no": "TR-2024", "year": "2024",
    },
    "dataset": {
        "id": 14, "url": "https://example.com/dataset", "source_type": "web",
        "type": "dataset", "title": "Ancient Books Dataset", "author": "Deng, J",
        "publisher": "Stanford", "year": "2009",
    },
    "manuscript": {
        "id": 15, "url": "https://example.com/ms", "source_type": "archive",
        "type": "manuscript", "title": "金陵梵刹志稿本", "author": "葛寅亮",
        "publisher_place": "南京", "publisher": "南京图书馆", "year": "1607",
    },
}


def _r(content, entries, style_id, **kw):
    return render_citations(content, entries, style_id=style_id, accessed=ACCESSED, **kw)


def _one(entry):
    """单元格夹具：单条渲染，正文引 [1] —— 条目 id 归一为 1。"""
    e = dict(entry)
    e["id"] = 1
    return e


def _family_intext_ok(family, rendered):
    c = rendered.content
    if family == "numeric-superscript":
        return "<sup>" in c
    if family == "numeric-bracket":
        return bool(re.search(r"\[\d+\]", c))
    if family in ("author-date", "author-page"):
        return ("(" in c) or ("（" in c)
    if family == "notes":
        return "<sup>" in c and len(rendered.notes) >= 1
    return False


# ---------- T1：11 样式 × 15 类型全矩阵 ----------

def test_full_discipline_matrix_cells():
    failures = []
    for style_id, family in FAMILY.items():
        for tname, entry in ENTRIES.items():
            ctx = f"{style_id}/{tname}"
            try:
                r = _r("正文 [1]。", [_one(entry)], style_id)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{ctx}: render raised {type(exc).__name__}: {exc}")
                continue
            if not str(r.bibliography_html).strip():
                failures.append(f"{ctx}: empty bibliography")
                continue
            title_token = entry["title"][:5]
            if title_token not in r.bibliography_html:
                failures.append(f"{ctx}: title {title_token!r} missing from bibliography")
            if not _family_intext_ok(family, r):
                failures.append(f"{ctx}: in-text family {family} form missing ({r.content[:60]!r})")
    assert not failures, "matrix failures (%d): %s" % (len(failures), "；".join(failures[:25]))


# ---------- T2：无 URL 古籍 × 11 样式 ----------

# Harvard (Cite Them Right) 的 book 格式不含卷次（其 CSL 规则如此）——卷次断言
# 对该样式豁免；标题/版本（刻本）/年份/出版项必须全样式照录（不降级、无 URL 泄漏）。
VOLUME_STYLES = set(FAMILY) - {"harvard"}


def test_no_url_ancient_book_all_styles():
    failures = []
    ancient = ENTRIES["ancient_book"]
    for style_id in FAMILY:
        r = _r("正文 [1]。", [_one(ancient)], style_id)
        bib = str(r.bibliography_html)
        ctx = f"{style_id}/no-url-ancient"
        if not bib.strip():
            failures.append(f"{ctx}: empty bibliography")
            continue
        tokens = ["摄山志", "刻本", "1790", "汪志伊"]
        if style_id in VOLUME_STYLES:
            tokens.append("卷四")
        for token in tokens:
            if token not in bib:
                failures.append(f"{ctx}: missing {token!r}")
        if "http" in bib.lower():
            failures.append(f"{ctx}: URL leaked for URL-less entry")
    # 出版地是样式相关字段（APA/MLA/Chicago 书式不含出版地）：以 GB 样式验证
    # publisher_place 映射端到端生效。
    gb = _r("正文 [1]。", [_one(ancient)], "gb-t-7714-numeric")
    if "金陵" not in str(gb.bibliography_html):
        failures.append("gb-t-7714-numeric: publisher_place 金陵 missing（字段映射断链）")
    assert not failures, "T2 failures (%d): %s" % (len(failures), "；".join(failures[:20]))


# ---------- M1：台账条目升格 ----------

def test_ledger_register_biblio_fields():
    led = CitationLedger()
    i = led.register(
        "", title="摄山志:八卷", author="陈毅", edition="刻本",
        publisher_place="金陵", publisher="汪志伊", year="1790", volume="卷四",
        source_type="book", genre="古籍",
    )
    entry = led.entries[i - 1]
    assert entry["edition"] == "刻本"
    assert entry["volume"] == "卷四"
    assert entry["publisher_place"] == "金陵"
    assert entry["source_type"] == "book"
    assert entry["year"] == "1790"


def test_ledger_url_less_dedupe_key():
    led = CitationLedger()
    i1 = led.register("", title="摄山志", author="陈毅", year="1790")
    i2 = led.register("", title="摄山志", author="陈毅", year="1790")
    assert i1 == i2, "same title+author+year must dedupe"
    i3 = led.register("", title="摄山志", author="陈毅", year="1791")
    assert i3 != i1, "different year must not dedupe"
    i4 = led.register("", title="宋论", author="王夫之", year="1790")
    assert i4 != i1


def test_ledger_url_less_multivolume_distinct():
    """C1（遗留清零）：同 title+author+year、不同卷次/版本/丛书 = 不同条目
    （多卷古籍不得合并）。"""
    led = CitationLedger()
    i1 = led.register("", title="摄山志", author="陈毅", year="1790", volume="卷四")
    i2 = led.register("", title="摄山志", author="陈毅", year="1790", volume="卷五")
    assert i1 != i2, "不同卷次必须视为不同条目"
    assert led.register("", title="摄山志", author="陈毅", year="1790", volume="卷四") == i1
    i3 = led.register("", title="摄山志", author="陈毅", year="1790", volume="卷四", edition="刻本")
    assert i3 != i1, "不同版本必须视为不同条目"
    i4 = led.register("", title="摄山志", author="陈毅", year="1790", volume="卷四", container="四库全书")
    assert i4 != i1, "不同丛书容器必须视为不同条目"


def test_ledger_key_helper():
    assert ledger_key("https://a.com/x/", "", "", "") == "https://a.com/x"
    k1 = ledger_key("", "摄山志", "陈毅", "1790")
    k2 = ledger_key("", "摄山志", "陈毅", "1790")
    assert k1 == k2 and k1
    assert ledger_key("", "摄山志", "陈毅", "1790", "刻本") != k1
    assert ledger_key("", "摄山志", "陈毅", "1790", "", "卷四") != k1


def test_register_hits_passthrough_biblio():
    led = CitationLedger()
    ids = led.register_hits([{
        "url": "https://shidianguji.com/book/x", "title": "摄山志",
        "snippet": "卷四", "author": "陈毅", "source_type": "gazetteer",
        "edition": "刻本",
    }])
    entry = led.entries[ids[0] - 1]
    assert entry.get("source_type") == "gazetteer"
    assert entry.get("edition") == "刻本"


# ---------- M3：CSL 类型与字段全映射 ----------

def test_csl_type_mapping_precedence():
    # 显式 type 优先
    assert csl_type_for_entry({"type": "chapter", "source_type": "book"}) == "chapter"
    # genre 映射
    assert csl_type_for_entry({"genre": "标准"}) == "standard"
    assert csl_type_for_entry({"genre": "专利"}) == "patent"
    # source_type 映射
    assert csl_type_for_entry({"source_type": "book"}) == "book"
    assert csl_type_for_entry({"source_type": "gazetteer"}) == "book"
    assert csl_type_for_entry({"source_type": "archive"}) == "manuscript"
    assert csl_type_for_entry({"source_type": "periodical"}) == "article-journal"
    # URL 推断兜底
    assert csl_type_for_entry({"url": "https://arxiv.org/abs/1706.03762"}) == "article-journal"
    assert csl_type_for_entry({"url": "https://example.com/x"}) == "webpage"


def test_entry_to_csl_url_only_when_present():
    from app.services.citation_style_service import _entry_to_csl

    item = _entry_to_csl(ENTRIES["ancient_book"], ACCESSED)
    assert "URL" not in item, "URL-less entry must not carry URL key"
    assert item["type"] == "book"
    assert item["edition"] == "刻本"
    assert item["volume"] == "卷四"
    assert item["publisher-place"] == "金陵"
    assert item["publisher"] == "汪志伊"
    assert item["issued"]["date-parts"] == [[1790]]

    item2 = _entry_to_csl(ENTRIES["webpage"], ACCESSED)
    assert item2["URL"] == "https://example.com/ai-report"


def test_entry_to_csl_title_fallback():
    from app.services.citation_style_service import _entry_to_csl

    item = _entry_to_csl(
        {"id": 99, "url": "", "source_type": "book", "container": "四库全书", "year": "1790"},
        ACCESSED,
    )
    assert item["title"].strip() != ""
    assert "四库全书" in item["title"]


# ---------- M4 预备：逐条 reference 渲染 ----------

def test_render_entry_references_no_url():
    refs = render_entry_references([ENTRIES["ancient_book"]], "gb-t-7714-numeric", accessed=ACCESSED)
    assert len(refs) == 1
    assert refs[0]["id"] == 3
    assert "刻本" in refs[0]["reference"]
    assert "http" not in refs[0]["reference"].lower()


def test_style_registry_still_11():
    assert len(STYLE_REGISTRY) == 11
