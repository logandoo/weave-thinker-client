"""W3 TDD：PDF 引擎去 AGPL（PyMuPDF 家族移除 → pdfplumber 主路径，pdfminer 兜底保留）。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_pdf_engine_permissive.py -v

设计：RED 阶段（已卸载 PyMuPDF、源码尚未改造时）期望 1/2/4 失败：
  1) 源码 guard —— file_parser 仍引用 pymupdf4llm（违反 permissive 约束）
  2) venv guard —— AGPL 包不得存在于运行环境
  4) 表格提取 —— 朴素 pdfminer 兜底无法产出 Markdown 表格（质量目标缺失）
GREEN 阶段：pdfplumber 主路径落地后全绿。
"""
import asyncio
import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import file_parser  # noqa: E402
import app.services.file_parser as FP  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.units import cm  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # noqa: E402
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle  # noqa: E402


def _make_pdf(path: str) -> str:
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    doc = SimpleDocTemplate(path, pagesize=A4)
    data = [["指标", "复合订单", "零售"],
            ["GMV环比", "11.7%", "6.9%"],
            ["利润", "9.7%", "4.8%"]]
    table = Table(data, colWidths=[4 * cm, 4 * cm, 4 * cm])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
    ]))
    story = [
        Paragraph("编织思维发布说明", style=ParagraphStyle_CJK()),
        Paragraph("本段落验证中文文字层抽取。", style=ParagraphStyle_CJK()),
        table,
        Paragraph("尾注 ALPHACHECK 结束段。", style=ParagraphStyle_CJK()),
    ]
    doc.build(story)
    return path


def ParagraphStyle_CJK():
    from reportlab.lib.styles import ParagraphStyle
    return ParagraphStyle("cjk", fontName="STSong-Light", fontSize=12, leading=18)


def _tmp_pdf(tmp_path, name="w37_fixture.pdf"):
    return _make_pdf(str(tmp_path / name))


def test_1_no_pymupdf_in_file_parser_source():
    src = open(FP.__file__, encoding="utf-8").read()
    assert "pymupdf" not in src.lower(), "file_parser 仍引用 AGPL 库 pymupdf*"
    assert "fitz" not in src, "file_parser 仍引用 fitz 别名"


def test_2_agpl_packages_absent_from_env():
    for mod in ("pymupdf", "pymupdf4llm", "fitz"):
        assert importlib.util.find_spec(mod) is None, f"venv 内仍存在 AGPL 包 {mod}"


def test_3_cjk_text_extracted(tmp_path):
    pdf = _tmp_pdf(tmp_path)
    md = file_parser.parse_pdf(pdf)
    assert "编织思维" in md
    assert "中文文字层抽取" in md
    assert "ALPHACHECK" in md


def test_4_table_extracted_as_markdown(tmp_path):
    pdf = _tmp_pdf(tmp_path)
    md = file_parser.parse_pdf(pdf)
    lines = [l for l in md.splitlines() if l.strip().startswith("|")]
    joined = "\n".join(lines)
    assert "复合订单" in joined and "11.7%" in joined, f"表格未以 Markdown 呈现: {joined[:200]}"
    assert "|" in md, "输出中没有任何 Markdown 表格行"


def test_5_garbage_pdf_degrades_gracefully(tmp_path):
    p = tmp_path / "garbage.pdf"
    p.write_bytes(b"%PDF-1.4\nthis is not a valid pdf body\n")
    res = asyncio.run(file_parser.parse_file(str(p), "garbage.pdf"))
    assert res["success"] is False
    assert res["error"]


def test_6_scanned_shortcut_still_pdfminer_backed():
    src = open(FP.__file__, encoding="utf-8").read()
    assert "pdfminer" in src, "扫描件短路口（is_image_pdf）的 pdfminer 基座被误删"


def test_7_pipe_in_cell_keeps_alignment(tmp_path):
    import io as _io
    import re as _re

    def _md_cols(line: str) -> int:
        inner = line.strip().strip('|')
        return len(_re.split(r'(?<!\\)\|', inner))

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    data = [["列A", "含|管道符", "列C"], ["v1", "x|y|z", "v3"]]
    tbl = Table(data, colWidths=[3 * cm, 5 * cm, 3 * cm])
    tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
    ]))
    buf = _io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4).build([tbl])
    p = tmp_path / "pipe.pdf"
    p.write_bytes(buf.getvalue())
    md = file_parser.parse_pdf(str(p))
    block = [l for l in md.splitlines() if l.strip().startswith('|')]
    assert block, "表格块缺失"
    counts = {_md_cols(l) for l in block}
    assert counts == {3}, f"每行应恰为 3 个未转义列: {block[:3]} counts={counts}"
    assert "x\\|y\\|z" in md and "含\\|管道符" in md, "管道符未转义"
