"""RED tests (A4.8) — office→PDF 转换服务（2026-09-19）。

契约（docs/BACKEND_DESIGN_workspace_preview_folders_20260919.html §6）：
  - SUPPORTED_OFFICE_EXT 白名单；
  - ensure_soffice：显式可执行路径优先，PATH 探测兜底，找不到返回 None；
  - convert_to_pdf：真实 soffice 转 PDF（%PDF 魔数）+ 按 (path,mtime,size,ext)
    缓存命中不重转；无 soffice → OfficePreviewFailed。

Run: cd backend && python -m pytest tests/test_office_preview_service.py -v
"""
import asyncio
import os
import shutil
import stat
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import office_preview_service as ops  # noqa: E402

REAL_SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")


def test_supported_extensions():
    assert ops.is_supported("a.docx")
    assert ops.is_supported("A.XLSX")
    assert ops.is_supported("deck.ppt")
    assert not ops.is_supported("a.pdf")
    assert not ops.is_supported("a.txt")
    assert not ops.is_supported("noext")


def test_ensure_soffice_explicit_path(tmp_path):
    fake = tmp_path / "soffice"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    assert asyncio.run(ops.ensure_soffice(str(fake))) == str(fake)
    assert asyncio.run(ops.ensure_soffice(str(tmp_path / "missing"))) is None


def test_convert_without_soffice_raises_unavailable(tmp_path):
    src = tmp_path / "a.docx"
    src.write_bytes(b"not-really")
    with pytest.raises(ops.OfficePreviewUnavailable):
        asyncio.run(ops.convert_to_pdf(
            src,
            soffice=str(tmp_path / "no-such-soffice"),
            cache_dir=tmp_path / "cache",
            timeout=10,
            max_cache_mb=64,
        ))


def test_convert_failure_raises_failed(tmp_path):
    src = tmp_path / "a.docx"
    src.write_bytes(b"not-really")
    fake = tmp_path / "soffice"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(ops.OfficePreviewFailed):
        asyncio.run(ops.convert_to_pdf(
            src,
            soffice=str(fake),
            cache_dir=tmp_path / "cache",
            timeout=10,
            max_cache_mb=64,
        ))


@pytest.mark.skipif(not REAL_SOFFICE, reason="soffice not installed")
def test_real_docx_conversion_and_cache(tmp_path):
    from docx import Document

    src = tmp_path / "hello.docx"
    doc = Document()
    doc.add_heading("预览测试", level=1)
    doc.add_paragraph("Hello office preview")
    doc.save(str(src))

    cache = tmp_path / "cache"
    first = asyncio.run(ops.convert_to_pdf(
        src, soffice=REAL_SOFFICE, cache_dir=cache, timeout=120, max_cache_mb=256,
    ))
    assert first.exists()
    assert first.read_bytes()[:5] == b"%PDF-"
    mtime = first.stat().st_mtime_ns

    time.sleep(0.02)
    second = asyncio.run(ops.convert_to_pdf(
        src, soffice=REAL_SOFFICE, cache_dir=cache, timeout=120, max_cache_mb=256,
    ))
    assert second == first
    assert second.stat().st_mtime_ns == mtime  # cache hit — no rewrite

    # mtime changes → new cache key → reconversion
    os.utime(src, (time.time() + 5, time.time() + 5))
    third = asyncio.run(ops.convert_to_pdf(
        src, soffice=REAL_SOFFICE, cache_dir=cache, timeout=120, max_cache_mb=256,
    ))
    assert third != first
    assert third.read_bytes()[:5] == b"%PDF-"


def test_cache_magic_validation(tmp_path):
    good = tmp_path / "good.pdf"
    good.write_bytes(b"%PDF-1.7 fake")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"<html>poisoned</html>")
    assert ops._is_valid_pdf(good) is True
    assert ops._is_valid_pdf(bad) is False
    assert ops._is_valid_pdf(tmp_path / "missing.pdf") is False


def test_cache_identity_shared_across_spool_copies(tmp_path):
    """A4.9 re-review: spool-named copies with the same workspace identity
    + fstat must share one cache entry (otherwise every request misses)."""
    identity = str(tmp_path / "workspace" / "doc.docx")
    a = ops._cache_key(identity, 111, 222, ".docx")
    b = ops._cache_key(identity, 111, 222, ".docx")
    c = ops._cache_key(identity, 111, 223, ".docx")
    d = ops._cache_key(str(tmp_path / "spool_a.docx"), 111, 222, ".docx")
    assert a == b
    assert a != c and a != d


def test_prune_stale_spools(tmp_path):
    import time as _time

    cache = tmp_path / "cache"
    cache.mkdir()
    fresh = cache / "wt_office_src_fresh.docx"
    fresh.write_bytes(b"x")
    stale = cache / "wt_office_src_stale.docx"
    stale.write_bytes(b"x")
    old = _time.time() - 7200
    os.utime(stale, (old, old))
    ops._prune_stale_spools(cache, max_age_seconds=3600)
    assert fresh.exists()
    assert not stale.exists()


def test_convert_target_single_page_for_calc():
    """A4.9/用户报告 2026-09-19：Calc 工作簿必须整表单页导出，避免图表被
    分页边界裁剪；其他格式保持普通 pdf。"""
    # .xlsx uses the version-independent print-setup patch → plain pdf
    assert ops.convert_target(".xlsx") == "pdf"
    assert ops.convert_target(".XLSX") == "pdf"
    for ext in (".xls", ".ods"):
        assert ops.convert_target(ext).startswith("pdf:calc_pdf_Export"), ext
    for ext in (".doc", ".docx", ".ppt", ".pptx", ".odt", ".odp", ".rtf"):
        assert ops.convert_target(ext) == "pdf", ext


def test_cache_key_carries_pipeline_version(monkeypatch):
    """转换管线变更必须使旧缓存失效（key 内嵌版本前缀）。"""
    original = ops._CACHE_VERSION
    a = ops._cache_key("src", 1, 2, ".xlsx")
    assert len(a) == 32
    monkeypatch.setattr(ops, "_CACHE_VERSION", "999")
    b = ops._cache_key("src", 1, 2, ".xlsx")
    assert a != b, "pipeline version must participate in the cache key"
    monkeypatch.setattr(ops, "_CACHE_VERSION", original)
    assert ops._cache_key("src", 1, 2, ".xlsx") == a


@pytest.mark.skipif(not REAL_SOFFICE, reason="soffice not installed")
def test_charted_workbook_exports_one_page_per_sheet(tmp_path):
    """图工作簿经 SinglePageSheets 导出后：每工作表 1 页（图表不被分页裁剪）。"""
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference

    src = tmp_path / "charted.xlsx"
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "数据"
    for i, v in enumerate([10, 20, 30, 40]):
        ws1.append([f"r{i}", v])
    ws2 = wb.create_sheet("图表")
    ws2.append(["k", "v"])
    for i, v in enumerate([5, 15, 25, 35]):
        ws2.append([f"k{i}", v])
    chart = BarChart()
    chart.add_data(Reference(ws2, min_col=2, min_row=1, max_row=5), titles_from_data=True)
    ws2.add_chart(chart, "D2")
    wb.save(src)

    pdf = asyncio.run(ops.convert_to_pdf(
        src,
        soffice=REAL_SOFFICE,
        cache_dir=tmp_path / "cache",
        timeout=120,
        max_cache_mb=256,
        cache_identity=str(src),
        cache_stat=(src.stat().st_mtime_ns, src.stat().st_size),
    ))
    raw = pdf.read_bytes()
    assert raw[:5] == b"%PDF-"
    pages = raw.count(b"/Type/Page") - raw.count(b"/Type/Pages")
    assert pages == 2, f"expected one page per sheet, got {pages}"


def test_needs_paginated_fallback_reads_mediabox(tmp_path):
    tall = tmp_path / "tall.pdf"
    tall.write_bytes(b"<</Type/Page/MediaBox [0 0 600 18000]>>")
    small = tmp_path / "small.pdf"
    small.write_bytes(b"<</Type/Page/MediaBox [0 0 600 842]>>")
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"%PDF- no boxes")
    assert ops._needs_paginated_fallback(tall) is True
    assert ops._needs_paginated_fallback(small) is False
    assert ops._needs_paginated_fallback(empty) is False  # unparseable → keep single-page


@pytest.mark.skipif(not REAL_SOFFICE, reason="soffice not installed")
def test_giant_sheet_falls_back_to_paginated(tmp_path):
    """A4.9 I-2：超大工作表回退分页导出，避免单页高度超出浏览器画布上限。"""
    from openpyxl import Workbook

    src = tmp_path / "giant.xlsx"
    wb = Workbook()
    ws = wb.active
    for i in range(300):
        ws.append([f"row{i}", i, i * 2, i * 3, i * 4])
    wb.save(src)

    pdf = asyncio.run(ops.convert_to_pdf(
        src,
        soffice=REAL_SOFFICE,
        cache_dir=tmp_path / "cache",
        timeout=180,
        max_cache_mb=256,
        cache_identity=str(src),
        cache_stat=(src.stat().st_mtime_ns, src.stat().st_size),
    ))
    sizes = ops._pdf_page_sizes(pdf.read_bytes())
    assert len(sizes) > 1, "giant sheet must not flatten to a single page"
    assert max(h for _w, h in sizes) <= ops._SINGLE_PAGE_MAX_PT + 60


def test_patch_xlsx_print_setup_rewrites_sheets(tmp_path):
    """打印设置补丁：sheetPr 内 fitToPage + pageSetup 1x1；其他 zip 条目原样保留。"""
    import zipfile

    from openpyxl import Workbook

    src = tmp_path / "book.xlsx"
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "S1"
    ws1.append(["a", "b"])
    ws2 = wb.create_sheet("S2")
    ws2.append(["c"])
    wb.save(src)

    with zipfile.ZipFile(src) as zf:
        names_before = set(zf.namelist())
    patched = ops._patch_xlsx_print_setup(src, tmp_path)
    assert patched != src and patched.exists()

    with zipfile.ZipFile(patched) as zf:
        names_after = set(zf.namelist())
        assert names_after == names_before, "zip entries must be preserved"
        sheets = [n for n in names_after if n.startswith("xl/worksheets/sheet")]
        assert len(sheets) == 2
        for name in sheets:
            xml = zf.read(name).decode("utf-8")
            assert 'fitToPage="1"' in xml, name
            assert 'fitToWidth="1"' in xml and 'fitToHeight="1"' in xml, name
            assert xml.count("fitToPage=") == 1, "no duplicate attributes"
    patched.unlink()


def test_xlsx_needs_pagination_row_guard(tmp_path):
    from openpyxl import Workbook

    small = tmp_path / "small.xlsx"
    wb = Workbook()
    wb.active.append(["x"])
    wb.save(small)
    assert ops._xlsx_needs_pagination(small) is False

    big = tmp_path / "big.xlsx"
    wb2 = Workbook()
    ws = wb2.active
    for i in range(ops._XLSX_FIT_MAX_ROWS + 5):
        ws.append([i])
    wb2.save(big)
    assert ops._xlsx_needs_pagination(big) is True


def test_patch_handles_paired_pagesetuppr_and_protection(tmp_path):
    """A4.9 fix8 Important-1/2：成对 <pageSetUpPr>…</pageSetUpPr> 不得产生
    孤立闭合标签；无 <sheetPr> 但有 <sheetProtection> 的工作表必须把
    pageSetUpPr 嵌进新建的 sheetPr 而不是被 <sheetProtection 前缀误匹配。"""
    import xml.etree.ElementTree as ET

    paired = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetPr><tabColor rgb=\"00FF0000\"/><pageSetUpPr/></sheetPr>"
        "<sheetData/></worksheet>"
    )
    out = ops._patch_sheet_fit_to_page(paired)
    ET.fromstring(out)  # well-formed
    assert out.count('fitToPage="1"') == 1 and "</pageSetUpPr>" not in out

    paired_closed = paired.replace("<pageSetUpPr/>", "<pageSetUpPr autoPageBreaks=\"1\"></pageSetUpPr>")
    out2 = ops._patch_sheet_fit_to_page(paired_closed)
    ET.fromstring(out2)
    assert out2.count("<pageSetUpPr") == 1, "paired element fully replaced"
    assert "</pageSetUpPr>" not in out2, "no orphan closing tag"
    assert out2.count('fitToPage="1"') == 1

    protected = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetProtection sheet="1" objects="1" scenarios="1" password="ABCD"/>'
        "<sheetData/></worksheet>"
    )
    out3 = ops._patch_sheet_fit_to_page(protected)
    ET.fromstring(out3)
    root = ET.fromstring(out3)
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    sheet_pr = root.find(f"{ns}sheetPr")
    assert sheet_pr is not None, "sheetPr must be created"
    psu = sheet_pr.find(f"{ns}pageSetUpPr")
    assert psu is not None and psu.get("fitToPage") == "1"
    assert root.find(f"{ns}sheetProtection") is not None


def test_patch_falls_back_on_malformed_rewrite(tmp_path, monkeypatch):
    """任何不能被 ElementTree 解析的改写必须整体放弃（返回原文件）。"""
    import zipfile

    from openpyxl import Workbook

    src = tmp_path / "book.xlsx"
    wb = Workbook()
    wb.active.append(["x"])
    wb.save(src)

    def broken(_xml: str) -> str:
        return "<worksheet><sheetPr></worksheet>"  # malformed

    monkeypatch.setattr(ops, "_patch_sheet_fit_to_page", broken)
    out = ops._patch_xlsx_print_setup(src, tmp_path)
    assert out == src
    assert not list(tmp_path.glob("*.wt_fit.xlsx"))
    with zipfile.ZipFile(src) as zf:
        assert zf.testzip() is None


# ---------------------------------------------------------------------------
# 2026-09-21: spreadsheet HTML preview（xlsx 长文本截断 / 宽表横拆修复）
# 契约：sanitize_office_html 白名单净化 + 本地图片内联；convert_to_html 走
# soffice `--convert-to html`，产物自包含并缓存；仅表格类扩展名可用。
# ---------------------------------------------------------------------------


def test_spreadsheet_whitelist():
    assert ops.is_spreadsheet("a.xlsx")
    assert ops.is_spreadsheet("B.XLS")
    assert ops.is_spreadsheet("c.ods")
    assert not ops.is_spreadsheet("a.docx")
    assert not ops.is_spreadsheet("a.csv")


def test_sanitize_html_strips_scripts_handlers_and_external_refs(tmp_path):
    html = (
        '<html><head><style>body{font-family:"Calibri"}</style>'
        "<script>alert(1)</script></head><body>"
        '<table><tr><td onclick="evil()">cell</td>'
        '<td><a href="http://evil.example/x">link</a></td>'
        '<td><a href="javascript:alert(2)">js</a></td>'
        '<td><a href="#table0" name="t0">jump</a></td>'
        '<td><img src="http://evil.example/track.png"></td>'
        '<td><img src="data:image/gif;base64,R0lGOD"></td>'
        '<td><iframe src="http://evil.example"></iframe></td>'
        '<td style="background:url(http://evil.example/x.png)">s</td>'
        '<td style="background:u\\72l(http://evil.example/x.png)">esc</td>'
        '<td data-sheets-value="{&quot;1&quot;:2}">v</td></tr></table>'
        '<form action="/x"><input name="a"></form>'
        "</body></html>"
    )
    out = ops.sanitize_office_html(html, tmp_path)
    for bad in (
        "<script", "onclick", "javascript:", "evil.example",
        "<iframe", "<form", "<input", "data-sheets-value", "u\\72l",
    ):
        assert bad not in out, f"sanitizer leaked {bad!r}"
    for keep in ("cell", "link", "jump", "esc"):
        assert keep in out
    assert 'href="#table0"' in out
    assert "data:image/gif;base64,R0lGOD" in out
    assert "body{font-family" in out  # LibreOffice stylesheet survives


def test_sanitize_rejects_css_escape_bypass(tmp_path):
    """CSS escapes decode before tokenizing, so a raw substring filter is not
    enough — any backslash must be refused outright (A4.9 2026-09-21)."""
    html = (
        "<html><head><style>body{background:u\\72l(https://evil.example/a.png)}</style></head>"
        '<body><table><tr><td style="color:red">ok</td>'
        '<td style="background:u\\72l(https://evil.example/b.png)">no</td></tr></table></body></html>'
    )
    out = ops.sanitize_office_html(html, tmp_path)
    assert "u\\72l" not in out and "evil.example" not in out
    assert 'style="color:red"' in out, "benign inline styles must survive"


def test_sanitize_enforces_asset_total_budget(tmp_path, monkeypatch):
    (tmp_path / "a.png").write_bytes(b"\x89PNG" + b"0" * 64)
    (tmp_path / "b.png").write_bytes(b"\x89PNG" + b"0" * 64)
    monkeypatch.setattr(ops, "_HTML_ASSET_TOTAL_MAX_BYTES", 100)
    html = (
        '<html><body><img src="a.png"><img src="b.png"></body></html>'
    )
    with pytest.raises(ops.OfficePreviewFailed):
        ops.sanitize_office_html(html, tmp_path)


def test_sanitize_rejects_oversized_raw_html(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "_HTML_MAX_RAW_CHARS", 200)
    with pytest.raises(ops.OfficePreviewFailed):
        ops.sanitize_office_html("<html><body>" + "x" * 300 + "</body></html>", tmp_path)


def test_sanitize_html_inlines_local_images(tmp_path):
    (tmp_path / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    out = ops.sanitize_office_html(
        '<html><body><img src="chart.png" width="100"></body></html>', tmp_path
    )
    assert "data:image/png;base64," in out
    assert 'src="chart.png"' not in out

    out2 = ops.sanitize_office_html(
        '<html><body><img src="missing.png"></body></html>', tmp_path
    )
    assert "<img" not in out2, "unresolvable images must be dropped"


def test_sanitize_inlines_percent_encoded_asset_names(tmp_path):
    """LibreOffice 7.3 (Linux) percent-encodes non-ASCII chart names in the
    HTML src; the on-disk export keeps the raw name (A4.9 2026-09-21)."""
    name = "用户分析报告_html_ab12cd.png"
    (tmp_path / name).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    from urllib.parse import quote

    out = ops.sanitize_office_html(
        f'<html><body><img src="{quote(name)}" width="100"></body></html>', tmp_path
    )
    assert "data:image/png;base64," in out
    assert quote(name) not in out


def test_sanitize_html_keeps_table_markup(tmp_path):
    html = (
        '<html><body><table cellspacing="0" border="0">'
        '<colgroup width="222"></colgroup>'
        '<tr><td height="35" align="left" valign="bottom">'
        '<b><font face="PingFang SC" size="5">T</font></b></td></tr>'
        "</table></body></html>"
    )
    out = ops.sanitize_office_html(html, tmp_path)
    assert "<table" in out and "<colgroup" in out and "<td" in out
    assert "PingFang SC" in out and "222" in out


def test_sanitize_html_enforces_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "_HTML_MAX_BYTES", 400)
    big = "<html><body>" + "<p>xxxx</p>" * 200 + "</body></html>"
    with pytest.raises(ops.OfficePreviewFailed):
        ops.sanitize_office_html(big, tmp_path)


def test_html_cache_magic_validation(tmp_path):
    good = tmp_path / "good.html"
    good.write_text(
        f'<!DOCTYPE html><html {ops._HTML_PREVIEW_MARKER}><body>x</body></html>',
        encoding="utf-8",
    )
    bad = tmp_path / "bad.html"
    bad.write_text('{"json": 1}', encoding="utf-8")
    unsanitized = tmp_path / "unsanitized.html"
    unsanitized.write_text("<!DOCTYPE html><html><body>raw</body></html>", encoding="utf-8")
    assert ops._is_valid_html(good) is True
    assert ops._is_valid_html(bad) is False
    assert ops._is_valid_html(unsanitized) is False, "must require the sanitizer marker"
    assert ops._is_valid_html(tmp_path / "missing.html") is False


@pytest.mark.skipif(not REAL_SOFFICE, reason="soffice not installed")
def test_real_xlsx_to_html_inlines_chart_and_caches(tmp_path):
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference

    src = tmp_path / "report.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "封面"
    ws["A1"] = "长文本" * 200
    ws2 = wb.create_sheet("图表")
    ws2.append(["k", "v"])
    for i, v in enumerate([5, 15, 25, 35]):
        ws2.append([f"k{i}", v])
    chart = BarChart()
    chart.add_data(Reference(ws2, min_col=2, min_row=1, max_row=5), titles_from_data=True)
    ws2.add_chart(chart, "D2")
    wb.save(src)

    cache = tmp_path / "cache"
    kwargs = dict(
        soffice=REAL_SOFFICE,
        cache_dir=cache,
        timeout=180,
        max_cache_mb=256,
        cache_identity=str(src),
        cache_stat=(src.stat().st_mtime_ns, src.stat().st_size),
    )
    first = asyncio.run(ops.convert_to_html(src, **kwargs))
    raw = first.read_text(encoding="utf-8")
    assert "<table" in raw and "长文本" in raw
    assert "data:image/png;base64," in raw, "chart image must be inlined"
    assert "<script" not in raw and "data-sheets-value" not in raw

    mtime = first.stat().st_mtime_ns
    time.sleep(0.02)
    second = asyncio.run(ops.convert_to_html(src, **kwargs))
    assert second == first
    assert second.stat().st_mtime_ns == mtime  # warm cache, no rewrite


def test_sanitize_injects_column_widths_and_fixed_layout(tmp_path):
    """LibreOffice HTML drops all but the first column width; the sanitizer
    re-injects the workbook's own widths with fixed layout so wide tables
    scroll horizontally instead of being squeezed/wrapped."""
    html = (
        '<html><body><table cellspacing="0" border="0">'
        '<colgroup width="10"></colgroup>'
        '<tr><td colspan="2">merged</td></tr>'
        "<tr><td>b</td><td>c</td></tr>"
        "</table></body></html>"
    )
    out = ops.sanitize_office_html(html, tmp_path, [[100, 200]])
    assert "table-layout:fixed" in out
    assert "width:300px" in out, "table width must be the sum of columns"
    assert out.count("width:100px") == 1 and out.count("width:200px") == 1
    assert 'colgroup width="10"' not in out, "stale LibreOffice colgroup must go"


def test_sanitize_skips_widths_when_source_incomplete(tmp_path):
    html = '<html><body><table><tr><td>a</td><td>b</td></tr></table></body></html>'
    out = ops.sanitize_office_html(html, tmp_path, [[100]])
    assert "table-layout:fixed" not in out
    out2 = ops.sanitize_office_html(html, tmp_path, None)
    assert "table-layout:fixed" not in out2


def test_sheet_column_widths_from_workbook(tmp_path):
    from openpyxl import Workbook

    src = tmp_path / "w.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "可见"
    ws["A1"] = "x"
    ws.column_dimensions["A"].width = 20
    hidden = wb.create_sheet("隐藏")
    hidden.sheet_state = "hidden"
    wb.save(src)

    plan = ops._sheet_column_widths(src)
    assert plan is not None
    names = [name for name, _widths in plan]
    assert names == ["可见", "隐藏"], "all sheets keep workbook order (name matching)"
    first = dict(plan)["可见"]
    assert first[0] == round(20 * 7 + 5)


def test_html_table_column_counts_account_for_colspan():
    html = (
        "<table><tr><td colspan='3'>t</td></tr><tr><td>a</td><td>b</td><td>c</td></tr></table>"
        "<table><tr><td>a</td><td>b</td></tr></table>"
    )
    assert ops._html_table_column_counts(html) == [3, 2]


def test_sanitize_drops_all_original_colgroups(tmp_path):
    """LibreOffice emits one colgroup per width run; every original colgroup
    (and col) must go once the workbook widths are injected, otherwise the
    stale HTML width attributes override the fixed layout."""
    html = (
        "<html><body><table>"
        '<colgroup width="10"></colgroup>'
        '<colgroup width="20"><col width="20"></colgroup>'
        "<tr><td>a</td><td>b</td></tr>"
        "</table></body></html>"
    )
    out = ops.sanitize_office_html(html, tmp_path, [[100, 200]])
    assert out.count("<colgroup>") == 1, "only the injected colgroup may remain"
    assert 'width="10"' not in out and 'width="20"' not in out
    assert "table-layout:fixed" in out


# ---------------- W19 抽函数共用（convert_workspace_document，S2 支撑）----------------

def test_convert_workspace_document_missing_source_raises_oserror(tmp_path):
    """源不可开/不存在 → OSError（调用方映射 404 / workspace_path_not_found）。"""
    missing = tmp_path / "nope.docx"
    with pytest.raises(OSError):
        asyncio.run(ops.convert_workspace_document(
            missing, kind="pdf", soffice=str(tmp_path / "no-soffice"),
            cache_dir=tmp_path / "cache", timeout=10, max_cache_mb=64,
        ))


def test_convert_workspace_document_directory_source_raises_oserror(tmp_path):
    """目录源 → OSError（非常规文件，与 files.py 冻结语义一致：不跟随/不转换）。"""
    d = tmp_path / "dir.docx"
    d.mkdir()
    with pytest.raises(OSError):
        asyncio.run(ops.convert_workspace_document(
            d, kind="pdf", soffice=str(tmp_path / "no-soffice"),
            cache_dir=tmp_path / "cache", timeout=10, max_cache_mb=64,
        ))


def test_convert_workspace_document_spool_failure_raises_preparation_error(tmp_path):
    """spool 冻结失败 → OfficePreviewPreparationError（调用方映射 500 / internal）。"""
    src = tmp_path / "a.docx"
    src.write_bytes(b"x")

    def boom(fd, suffix, cache_dir):
        raise OSError("disk full")

    saved = ops.spool_fd_to_temp
    ops.spool_fd_to_temp = boom
    try:
        with pytest.raises(ops.OfficePreviewPreparationError):
            asyncio.run(ops.convert_workspace_document(
                src, kind="pdf", soffice=str(tmp_path / "no-soffice"),
                cache_dir=tmp_path / "cache", timeout=10, max_cache_mb=64,
            ))
    finally:
        ops.spool_fd_to_temp = saved


def test_convert_workspace_document_no_soffice_raises_unavailable(tmp_path):
    """soffice 缺失 → OfficePreviewUnavailable（调用方映射 501 / office_preview_unavailable）。"""
    src = tmp_path / "a.docx"
    src.write_bytes(b"x")
    with pytest.raises(ops.OfficePreviewUnavailable):
        asyncio.run(ops.convert_workspace_document(
            src, kind="pdf", soffice=str(tmp_path / "no-soffice"),
            cache_dir=tmp_path / "cache", timeout=10, max_cache_mb=64,
        ))


def test_convert_workspace_document_convert_failure_raises_failed(tmp_path):
    """转换失败（假 soffice 非零退出）→ OfficePreviewFailed（映射 422 / office_preview_failed）。"""
    src = tmp_path / "a.docx"
    src.write_bytes(b"not-really-docx")
    fake = tmp_path / "soffice"
    fake.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(ops.OfficePreviewFailed):
        asyncio.run(ops.convert_workspace_document(
            src, kind="pdf", soffice=str(fake),
            cache_dir=tmp_path / "cache", timeout=10, max_cache_mb=64,
        ))


def test_convert_workspace_document_unsupported_kind_rejected(tmp_path):
    """kind 仅 pdf|html（参数面钉）。"""
    src = tmp_path / "a.docx"
    src.write_bytes(b"x")
    with pytest.raises(ValueError):
        asyncio.run(ops.convert_workspace_document(
            src, kind="txt", soffice="soffice",
            cache_dir=tmp_path / "cache", timeout=10, max_cache_mb=64,
        ))


@pytest.mark.skipif(not REAL_SOFFICE, reason="soffice not installed")
def test_convert_workspace_document_real_docx_to_pdf_and_spool_cleanup(tmp_path):
    """真 docx → PDF（%PDF-）；转换后 spool 副本必须已删（wt_office_src_* 清零）。"""
    from docx import Document

    src = tmp_path / "hello.docx"
    doc = Document()
    doc.add_heading("共用链路", level=1)
    doc.add_paragraph("shared convert")
    doc.save(str(src))

    cache = tmp_path / "cache"
    out = asyncio.run(ops.convert_workspace_document(
        src, kind="pdf", soffice=REAL_SOFFICE,
        cache_dir=cache, timeout=120, max_cache_mb=256,
    ))
    assert out.read_bytes()[:5] == b"%PDF-"
    spools = list(cache.glob("wt_office_src_*"))
    assert not spools, f"spool 副本必须清理: {spools}"


@pytest.mark.skipif(not REAL_SOFFICE, reason="soffice not installed")
def test_convert_workspace_document_real_xlsx_to_html(tmp_path):
    """真 xlsx → 自包含 HTML（表格标记）；spool 清理同上。"""
    from openpyxl import Workbook

    src = tmp_path / "sheet.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["名称", "数值"])
    ws.append(["共用", 7])
    wb.save(str(src))

    cache = tmp_path / "cache"
    out = asyncio.run(ops.convert_workspace_document(
        src, kind="html", soffice=REAL_SOFFICE,
        cache_dir=cache, timeout=120, max_cache_mb=256,
    ))
    text = out.read_text(encoding="utf-8", errors="replace")
    assert "<table" in text.lower()
    assert not list(cache.glob("wt_office_src_*"))


def test_convert_workspace_document_convert_stage_oserror_is_preparation_error(tmp_path):
    """评审 I-1（PoC 实证）：转换阶段 I/O 故障（缓存写/字体重建）不得冒充源不可达
    OSError→404——必须归 OfficePreviewPreparationError（HTTP 500 族）。"""
    src = tmp_path / "a.docx"
    src.write_bytes(b"x")

    def boom_convert(*args, **kwargs):
        raise OSError(28, "No space left on device")

    saved = ops.convert_to_pdf
    ops.convert_to_pdf = boom_convert
    try:
        with pytest.raises(ops.OfficePreviewPreparationError) as ei:
            asyncio.run(ops.convert_workspace_document(
                src, kind="pdf", soffice="soffice",
                cache_dir=tmp_path / "cache", timeout=10, max_cache_mb=64,
            ))
        assert "No space left" not in str(ei.value), "不得回显 OS 细节/路径给调用方"
    finally:
        ops.convert_to_pdf = saved
