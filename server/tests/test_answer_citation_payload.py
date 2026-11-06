"""M4 — 聊天出货 citations 载荷（服务端样式化 reference + in_text 映射）。

plan_citation_full_discipline_ref_20261011 M4：
- 终稿出货产出 citations 载荷：逐条规范 reference（用户所选样式）+ 文内族形态；
- 无 URL 古籍条目载荷可显示（卡片无链接）；
- content 保持 [N]（D-1 ADR）；零引用不产出（F3）；幂等。

Run: cd backend && python -m pytest tests/test_answer_citation_payload.py -q
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.chat import _render_answer_citations

ANCIENT = {
    "id": 1, "url": "", "source_type": "book", "genre": "古籍",
    "title": "摄山志:八卷", "author": "陈毅", "edition": "刻本",
    "publisher_place": "金陵", "publisher": "汪志伊", "year": "1790",
    "volume": "卷四",
}
WEB = {
    "id": 2, "url": "https://example.com/r", "source_type": "web",
    "title": "中国人工智能产业发展报告", "author": "工信部",
    "published_date": "2025-03-01",
}


def _tr(*entries):
    return json.dumps({"results": list(entries)}, ensure_ascii=False)


def test_payload_web_entry():
    content = "报告称产业规模扩大 [2]。"
    new_content, new_tr = asyncio.run(_render_answer_citations(content, _tr(WEB), "gb-t-7714-numeric"))
    assert new_content == content, "content 必须保持 [N] 撰写语法"
    obj = json.loads(new_tr)
    cit = obj["citations"]
    assert cit["style_id"] == "gb-t-7714-numeric"
    assert cit["family"] == "numeric-superscript"
    assert len(cit["entries"]) == 1
    e = cit["entries"][0]
    assert e["id"] == 2 and e["num"] == 1
    assert "中国人工智能产业发展报告" in e["reference"]
    assert e["url"] == "https://example.com/r"
    assert "[1]" in e["in_text"]
    assert e["title"] == "中国人工智能产业发展报告"


def test_payload_no_url_ancient_entry():
    content = "《摄山志》卷四有记载 [1]。"
    _, new_tr = asyncio.run(_render_answer_citations(content, _tr(ANCIENT), "gb-t-7714-numeric"))
    cit = json.loads(new_tr)["citations"]
    e = cit["entries"][0]
    assert e["url"] == ""
    assert "刻本" in e["reference"]
    assert "摄山志" in e["reference"]
    assert "http" not in e["reference"].lower()


def test_payload_author_date_family_in_text():
    content = "报告指出 [2]。"
    _, new_tr = asyncio.run(_render_answer_citations(content, _tr(WEB), "apa"))
    cit = json.loads(new_tr)["citations"]
    assert cit["family"] == "author-date"
    assert "(" in cit["entries"][0]["in_text"] or "（" in cit["entries"][0]["in_text"]


def test_payload_zero_citations_no_payload():
    content = "本文无任何引用标记。"
    new_content, new_tr = asyncio.run(_render_answer_citations(content, _tr(WEB), "apa"))
    assert new_content == content
    assert "citations" not in json.loads(new_tr)


def test_payload_idempotent():
    content = "报告称增长 [2]。"
    _, tr1 = asyncio.run(_render_answer_citations(content, _tr(WEB), "ieee"))
    _, tr2 = asyncio.run(_render_answer_citations(content, tr1, "apa"))
    cit1 = json.loads(tr1)["citations"]
    cit2 = json.loads(tr2)["citations"]
    assert cit1 == cit2, "已有载荷不得被二次渲染覆盖"


def test_payload_default_style_when_none():
    content = "报告称增长 [2]。"
    _, new_tr = asyncio.run(_render_answer_citations(content, _tr(WEB), None))
    cit = json.loads(new_tr)["citations"]
    assert cit["style_id"] == "gb-t-7714-numeric"  # resolve_style_id 默认


def test_payload_numeric_references_numbering_aligned():
    """数值族逐条 reference 编号必须与条目序号一致（不得每条都从 [1] 起）。"""
    content = "先引古籍 [1]，再引报告 [2]。"
    _, new_tr = asyncio.run(_render_answer_citations(content, _tr(ANCIENT, WEB), "gb-t-7714-numeric"))
    entries = json.loads(new_tr)["citations"]["entries"]
    assert len(entries) == 2
    assert entries[0]["reference"].strip().startswith("[1]")
    assert entries[1]["reference"].strip().startswith("[2]"), entries[1]["reference"]


def test_payload_duplicate_ids_first_wins():
    """C4（遗留清零）：results[] 重复 id（畸形数据）首见胜——不得 last-wins
    串用他人 reference。"""
    dup = [
        {"id": 1, "url": "https://a.com/1", "title": "甲条目", "snippet": "s1"},
        {"id": 1, "url": "https://a.com/2", "title": "乙条目", "snippet": "s2"},
    ]
    _, new_tr = asyncio.run(_render_answer_citations("正文 [1]。", _tr(*dup), "gb-t-7714-numeric"))
    entries = json.loads(new_tr)["citations"]["entries"]
    assert len(entries) == 1
    assert "甲条目" in entries[0]["reference"], "重复 id 必须首见胜"


def test_biblio_keys_registerable_contract():
    """C4 契约：extract_biblio_hints 产出的键必须可被 register 接受
    （BIBLIO_FIELDS 两侧同源，防未来漂移致 live 静默丢源）。"""
    import inspect

    from app.services.citation_ledger import BIBLIO_FIELDS, CitationLedger
    from app.services.citation_sources import extract_biblio_hints

    sig = inspect.signature(CitationLedger.register)
    for k in BIBLIO_FIELDS:
        assert k in sig.parameters, f"BIBLIO_FIELDS 键 {k} 不在 register 签名"
    for sample in (
        "王夫之.宋论[M].刻本.金陵:湘乡曾国荃,1865",
        "陈毅.摄山志:八卷[M].刻本.金陵:汪志伊,1790",
    ):
        hints = extract_biblio_hints(sample)
        extra = set(hints) - {"title", "snippet"}
        assert extra <= set(BIBLIO_FIELDS), f"{sample}: {extra - set(BIBLIO_FIELDS)}"


def test_payload_author_date_references_have_no_number_prefix():
    content = "先引报告 [2]。"
    _, new_tr = asyncio.run(_render_answer_citations(content, _tr(WEB), "apa"))
    ref = json.loads(new_tr)["citations"]["entries"][0]["reference"]
    assert not ref.strip().startswith("[1]"), ref
