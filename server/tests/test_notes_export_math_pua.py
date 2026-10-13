"""导出层数学 PUA 解码（2026-10-04 往返波配套）。

前端 serializeMathDelimited 对闭符碰撞 tex 会把 \\)/\\]/换行映射进私用区
（\\uE000-\\uE003，输入字面 PUA 走 \\uE005-8 转义层）。前端 tokenizer 捕获后
解码还原；导出层（PDF/HTML 数学保护 pass）读的是**原始 markdown**，必须同点
解码（notes.py `_decode_math_pua`），否则 MathJax 收到的源与前端 KaTeX 不同。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.notes import _decode_math_pua, _markdown_to_html_with_mermaid  # noqa: E402


def test_decode_pua_closers_exact():
    # 闭合标签制（与前端 decodeMathClosers 同表）
    assert _decode_math_pua('a\uE0FF1b') == 'a\\)b'
    assert _decode_math_pua('a\uE0FF2b') == 'a\\]b'
    assert _decode_math_pua('a\uE0FF3b') == 'a]b'
    assert _decode_math_pua('a\uE0FF4b') == 'a\nb'
    assert _decode_math_pua('a\uE0FF5b') == 'a\rb'


def test_decode_pua_escape_layer_exact():
    # 输入字面标记符经转义层往返（闭合转义=任意输入恒等）
    assert _decode_math_pua('z\uE0FF0z') == 'z\uE0FFz'
    assert _decode_math_pua('z\uE0FF00z') == 'z\uE0FF0z'


def test_decode_plain_passthrough():
    assert _decode_math_pua('x^2 + \\frac{1}{2}') == 'x^2 + \\frac{1}{2}'


def test_export_pua_newline_equals_decoded_source():
    # 可观察差异的等价性：tag4（换行）源与真换行源导出必须逐字节一致
    pua_md = '\n\\(x\uE0FF4y\\)\n\n'
    plain_md = '\n\\(x\ny\\)\n\n'
    assert _markdown_to_html_with_mermaid(pua_md) == _markdown_to_html_with_mermaid(plain_md)


def test_export_pua_closer_equals_decoded_source():
    # 同 latex `a\)b` 的两种载体等价：tag1 编码行内 vs 方括号形态裸闭符
    # （裸 `\(a\)b\)` 本身在任何提取正则下都会 mis-pair——那正是编码的存在理由，
    #  故 plain 侧用 \[...\] 承载同源 latex）
    # plain 侧用 $…$ 载体（同 latex、同 display=False；方括号载体是 display=True 会差 wrapper）
    pua_md = '\n\\(a\uE0FF1b\\)\n\n'
    plain_md = '\n$a\\)b$\n\n'
    assert _markdown_to_html_with_mermaid(pua_md) == _markdown_to_html_with_mermaid(plain_md)


def test_export_plain_math_still_renders():
    html = _markdown_to_html_with_mermaid('\n\\(x^2\\)\n\n')
    assert 'math-inline' in html or 'svg' in html, html
