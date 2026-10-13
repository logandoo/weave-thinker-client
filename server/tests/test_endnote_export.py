"""导出面尾注渲染（wave-2 A3）：前端尾注语法 [^id] / [^id]: 在笔记导出
（PDF/HTML）中必须渲染为角标 + 文末定义段，而非原始符号。

口径与前端 useMarkdown.preprocessEndnoteSyntax 对齐：
- 定义行抽取（围栏代码块内除外）
- 引用按首次出现序编号，显示〔N〕
- 定义正文走行内渲染保格式
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.notes import _extract_endnotes, _render_endnote_defs, _md_it  # noqa: E402


def test_extract_refs_and_defs():
    md = "正文甲[^1]乙[^2]\n\n[^1]: 一\n\n[^2]: 二\n"
    text, defs = _extract_endnotes(md)
    assert "[^1]" not in text
    assert "note-endnote-ref" in text
    assert "〔1〕" in text and "〔2〕" in text
    assert [(d, n) for d, n, _ in defs] == [("1", 1), ("2", 2)]
    assert defs[0][2].strip() == "一"


def test_numbering_follows_first_occurrence():
    md = "先[^b]后[^a]\n\n[^a]: A\n\n[^b]: B\n"
    _text, defs = _extract_endnotes(md)
    order = {d: n for d, n, _ in defs}
    assert order == {"b": 1, "a": 2}


def test_fenced_literal_not_converted():
    md = "```\n[^1] code\n```\n\n正文[^1]\n"
    text, _defs = _extract_endnotes(md)
    assert "`[^1] code`" in text or "[^1] code" in text
    assert "note-endnote-ref" in text


def test_defs_rendered_html():
    _text, defs = _extract_endnotes("正文[^7]\n\n[^7]: 见 **RFC**\n")
    html = _render_endnote_defs(defs)
    assert 'class="note-endnote-def"' in html
    assert "note-endnote-back" in html
    assert "RFC" in html and "<strong>" in html  # 正文格式保真（禁 textContent 扁平化）


def test_full_render_contains_markers():
    md = "正文[^3]\n\n[^3]: 尾注说明\n"
    text, defs = _extract_endnotes(md)
    html = _md_it.render(text) + _render_endnote_defs(defs)
    assert "note-endnote-ref" in html
    assert "note-endnote-def" in html
    assert "尾注说明" in html
