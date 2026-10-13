#!/usr/bin/env python3
"""Office 预览 CJK 字体修复测试（2026-09-29 用户报告：docx 中文完全无法显示）。

契约（tests/acceptance.md 本波判据）：
  - ensure_fontconfig：生成 fonts.conf——含系统/随包 Fonts 目录、宋体/黑体 别名、
    zh 语言回退规则；
  - 转换链路：_convert_once 注入 FONTCONFIG_FILE；转换出的 PDF 必须含 CJK 字体
    （Noto/Songti/PingFang/…）且不含替换出的 FrankRuhlHofshi（希伯来字体）；
  - 真实用户文件（红颊草莓栽培技术资料_中文翻译.docx）同样转出可读中文；
  - 缓存版本已升（v9），旧的缺字缓存 PDF 失效。

Run: .localstack/venv/bin/python server/tests/test_office_preview_cjk.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.office_preview_service import (  # noqa: E402
    _CACHE_VERSION,
    convert_to_pdf,
    ensure_fontconfig,
)

RESULTS = []


def check(desc, ok, detail=""):
    RESULTS.append((desc, bool(ok), detail))
    print(f"  {'ok  ' if ok else 'FAIL'} {desc}" + (f" ({detail})" if detail and not ok else ""))


def detect_soffice() -> str | None:
    cand = os.environ.get("SOFFICE_BIN")
    if cand and os.path.isfile(cand):
        return cand
    bundled = "/Applications/WeaveThinker.app/Contents/Resources/LibreOffice.app/Contents/MacOS/soffice"
    if os.path.isfile(bundled):
        return bundled
    return shutil.which("soffice") or shutil.which("libreoffice")


def make_cjk_docx(path: Path, text: str, font: str = "宋体") -> None:
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:rPr>"
        f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:eastAsia="{font}"/>'
        f"</w:rPr><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)


def pdf_fonts_and_text(pdf_path: Path):
    import pdfplumber

    with pdfplumber.open(str(pdf_path)) as pdf:
        fonts, text = set(), ""
        for page in pdf.pages[:3]:
            for c in page.chars:
                fonts.add(str(c.get("fontname") or ""))
            text += page.extract_text() or ""
    return fonts, text


def main() -> int:
    soffice = detect_soffice()
    if not soffice:
        print("  FAIL: no soffice binary found（不得空过——办公预览为交付能力）")
        return 1
    print(f"  soffice: {soffice}")

    tmp = Path(tempfile.mkdtemp(prefix="cjk-font-"))
    try:
        # ── 判据 1：fonts.conf 生成 ──
        conf = ensure_fontconfig(soffice, out_dir=tmp / "fc")
        conf_txt = conf.read_text(encoding="utf-8")
        from app.core.config import get_config

        bundled_fonts = str(get_config().resolve_resource("Fonts"))
        check("fonts.conf 含随包 Fonts 目录（NotoSansCJKsc 所在）",
              bundled_fonts in conf_txt, conf_txt[:200])
        check("fonts.conf 含系统字体目录", "/System/Library/Fonts" in conf_txt or "/usr/share/fonts" in conf_txt)
        check("fonts.conf 含宋体别名且链尾是必有的 Noto Sans CJK SC",
              "宋体" in conf_txt and "Noto Sans CJK SC" in conf_txt)
        check("fonts.conf 含通用族映射（serif/sans-serif/monospace）",
              "sans-serif" in conf_txt and "<prefer>" in conf_txt)
        check("fonts.conf 含 zh 语言回退", 'compare="contains"><string>zh</string>' in conf_txt)
        check("缓存版本 ≥9（旧缺字缓存失效）", int(_CACHE_VERSION) >= 9, str(_CACHE_VERSION))

        # ── 判据 2：合成 CJK docx（宋体）→ PDF 含 CJK 字体 ──
        docx = tmp / "cjk.docx"
        make_cjk_docx(docx, "中文测试红颊草莓栽培技术")
        cache = tmp / "cache"
        pdf = asyncio.run(convert_to_pdf(docx, soffice=soffice, cache_dir=cache, timeout=90))
        fonts, text = pdf_fonts_and_text(pdf)
        cjk_fonts = [f for f in fonts if any(
            k in f for k in ("NotoSansCJK", "NotoSerifCJK", "Songti", "PingFang", "Heiti", "STSong", "Kaiti", "SimSun", "SimHei", "Microsoft YaHei")
        )]
        check("合成 docx 转 PDF 含 CJK 字体", bool(cjk_fonts), f"fonts={sorted(fonts)}")
        check("合成 docx 中文文本可提取", "中文测试红颊草莓" in text.replace(" ", ""), f"text={text[:60]!r}")
        check("无 FrankRuhlHofshi（替换错字体消失）", not any("FrankRuhl" in f for f in fonts), f"fonts={sorted(fonts)}")

        # ── 判据 3：用户真实文件 ──
        real = (Path.home() / "Library/Application Support/weave-thinker-client/localstack/user_workspaces"
                / "alice-beef0002/红颊草莓栽培技术资料_中文翻译.docx")
        if real.is_file():
            pdf2 = asyncio.run(convert_to_pdf(real, soffice=soffice, cache_dir=cache, timeout=120))
            fonts2, text2 = pdf_fonts_and_text(pdf2)
            cjk2 = [f for f in fonts2 if any(
                k in f for k in ("NotoSansCJK", "NotoSerifCJK", "Songti", "PingFang", "Heiti", "STSong", "Kaiti", "SimSun", "SimHei")
            )]
            check("真实用户 docx 转 PDF 含 CJK 字体", bool(cjk2), f"fonts={sorted(fonts2)}")
            check("真实用户 docx 中文可提取（红颊草莓）",
                  "红" in text2 and "草莓" in text2, f"text={text2[:60]!r}")
            check("真实用户 docx 无 FrankRuhlHofshi", not any("FrankRuhl" in f for f in fonts2),
                  f"fonts={sorted(fonts2)}")
        else:
            print(f"  skip  真实用户 docx 不在本机（{real}）——判据 3 仅在有该文件的环境核验")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [d for d, ok, _ in RESULTS if not ok]
    print(f"\n  result: {'PASS' if not failed else 'FAIL'} ({len(RESULTS) - len(failed)}/{len(RESULTS)})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
