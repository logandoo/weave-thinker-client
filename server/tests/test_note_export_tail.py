"""RED tests (A4.8) — 笔记 PDF 导出：echarts 图表块后的内容被吞（conv b8dcedc8，问题2）。

根因链（实证见 tests/bisect_issue2*.py / tests/repro_issue2_export.py）：
  1. ECharts SVG 内含 `<style ><![CDATA[…hover 规则…]]></style>`（CDATA 内有空行）；
  2. `_markdown_to_html_with_mermaid` 第 0 步把渲染后的 SVG **直接内联**进 markdown
     文本流（违背该管线自身「先占位后渲染」设计），markdown-it 在 CDATA 空行处终止
     raw-HTML 块，把残留 `]]>` 包成 `<p>]]&gt;</p>`（CDATA 终止符被转义）；
  3. WeasyPrint 解析时 `<![CDATA[` 无终止符 → CDATA 吞到 EOF，后续全部内容进入
     `{svg}style` 节点（实证：tail 文本在 style 元素内）→ PDF 尾部整体消失。

Run: cd backend && python -m pytest tests/test_note_export_tail.py -v
"""
import io
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pdfplumber  # noqa: E402
import pytest  # noqa: E402

from app.api import notes as notes_api  # noqa: E402
from app.api.notes import _render_note_pdf  # noqa: E402

# 真实 echarts SVG 的危险形态：style CDATA + 内部空行（渲染器原样输出）。
FIXTURE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="460.0pt" height="287.5pt" '
    'viewBox="0 0 800 500">'
    '<text x="10" y="20">CHART_TITLE_MARK</text>'
    "<style ><![CDATA[\n"
    ".z0-cls-0:hover {\n"
    "cursor:pointer;\n"
    "}\n"
    "\n"
    "]]>\n"
    "\n"
    "</style>"
    "</svg>"
)


def _render(content: str) -> str:
    note = types.SimpleNamespace(title="t", content=content, created_at=None)
    pdf = _render_note_pdf(note, None)
    with pdfplumber.open(io.BytesIO(pdf)) as p:
        return "\n".join((pg.extract_text() or "") for pg in p.pages)


def test_tail_after_echarts_fence_survives_pdf(monkeypatch):
    """验收 #4：```echarts 图表块后的段落/标记不得被吞。"""
    monkeypatch.setattr(
        notes_api, "_batch_render_echarts_svg",
        lambda batch: {k: FIXTURE_SVG for k, _ in batch},
    )
    text = _render(
        "前文一段。\n\n```echarts\n{\"title\": {\"text\": \"x\"}}\n```\n\n"
        "TAIL_MARKER_XYZ 之后的内容必须出现在导出里。\n"
    )
    assert "TAIL_MARKER_XYZ" in text, f"tail swallowed by chart block: {text[-300:]!r}"


def test_chart_itself_still_renders(monkeypatch):
    """图表本体仍渲染（SVG 文本进 PDF），防「删图表保尾部」式假修复。"""
    monkeypatch.setattr(
        notes_api, "_batch_render_echarts_svg",
        lambda batch: {k: FIXTURE_SVG for k, _ in batch},
    )
    text = _render(
        "前文。\n\n```echarts\n{}\n```\n\nTAIL_MARKER_XYZ\n"
    )
    assert "CHART_TITLE_MARK" in text, text[:300]


def test_tail_after_second_chart_survives(monkeypatch):
    """双图表 + 中间表格/文段 + 尾章（生产笔记形态）全部存活。

    断言标记一律 ASCII（CI ubuntu-latest 无 CJK 字体，CJK PDF 文本抽取不可移植；
    真实生产内容的 CJK 全量验证由 tests/workflows 真实 HTTP 导出取证承担）。
    """
    monkeypatch.setattr(
        notes_api, "_batch_render_echarts_svg",
        lambda batch: {k: FIXTURE_SVG for k, _ in batch},
    )
    content = (
        "开头。\n\n```echarts\n{}\n```\n\n"
        "注：上段 JSON 说明文字 NOTE_MARK_A。\n\n"
        "| 日期 | 值 |\n| --- | --- |\n| 09-03 | 3.53 |\n| 09-27 | 3.57 |\n\n"
        "第二段说明 TAIL_PARA_MARK。\n\n```echarts\n{}\n```\n\n"
        "### 五、TAIL_SECTION_FIVE\n\n- 要点甲。\n\n"
        "### 七、TAIL_SECTION_SEVEN\n\n- 要点乙。\n"
    )
    text = _render(content)
    for marker in ("NOTE_MARK_A", "09-27", "TAIL_PARA_MARK", "TAIL_SECTION_FIVE", "TAIL_SECTION_SEVEN"):
        assert marker in text, f"missing {marker!r}: …{text[-400:]!r}"


def test_html_body_keeps_tail_outside_svg_style(monkeypatch):
    """HTML 边界断言（问题2机制级）：尾部不得落入 <style>/<svg> 内部。"""
    import xml.etree.ElementTree as ET

    monkeypatch.setattr(
        notes_api, "_batch_render_echarts_svg",
        lambda batch: {k: FIXTURE_SVG for k, _ in batch},
    )
    note = types.SimpleNamespace(
        title="t",
        content="前文。\n\n```echarts\n{}\n```\n\nTAIL_MARKER_XYZ\n",
        created_at=None,
    )
    html_body = notes_api._markdown_to_html_with_mermaid(note.content)
    # tail 文本必须在文档流，而非被解析进 style/svg 子树
    assert "TAIL_MARKER_XYZ" in html_body
    # 良构检查：若 tail 落进 {svg}style 说明 CDATA 吞噬复发
    wrapped = f"<root>{html_body}</root>"
    try:
        root = ET.fromstring(wrapped)
        for el in root.iter():
            if el.tag.endswith("style") or el.tag.endswith("svg"):
                assert "TAIL_MARKER_XYZ" not in ET.tostring(el, encoding="unicode"), (
                    f"tail nested inside <{el.tag}> — CDATA swallow regression"
                )
    except ET.ParseError:
        # 结构本身不合法也算失败——导出 HTML 必须是良构的
        pytest.fail(f"export HTML not well-formed: {wrapped[:400]!r}")
