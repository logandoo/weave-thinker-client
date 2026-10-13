"""导出层代码围栏长度感知（2026-10-03 遗留项⑭后端配套）。

前端 buildCodeFence 对内嵌 ``` 的代码会发 4+ 反引号围栏（GFM）；导出层
`_markdown_to_html_with_mermaid` 的数学保护 pass 原先用 3 反引号定长正则，
会在 4 围栏块内的 ``` 处提前闭合 → 代码被切碎、语言丢失。本测试钉住
围栏长度感知（backreference 等长闭合）行为。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.notes import _markdown_to_html_with_mermaid  # noqa: E402


def test_four_backtick_fence_roundtrips_code_body():
    inner = "```\ninner fence\n```"
    md = "\n````md\n" + inner + "\n````\n\n"
    html = _markdown_to_html_with_mermaid(md)
    assert "inner fence" in html, html
    # 语言标记保留（language-md），不被 inner ``` 打断
    assert 'class="language-md"' in html or 'language-md' in html, html
    # 代码体完整在同一个 <pre> 内，不出现孤立的裸 ``` 文本段
    assert html.count("inner fence") == 1, html


def test_three_backtick_fence_still_works():
    md = "\n```python\nprint(1)\n```\n\n"
    html = _markdown_to_html_with_mermaid(md)
    assert "print(1)" in html, html
    assert "language-python" in html, html


def test_blank_lines_inside_fence_preserved():
    md = "\n```sh\n\n\nfoo\n\n\n```\n\n"
    html = _markdown_to_html_with_mermaid(md)
    # 语义 oracle=marked 往返（前端 G2 实证）：围栏行之间的行以 \n 连接 =
    # "\n\nfoo\n\n"。（初稿断言误写 \n\n\nfoo\n\n\n，与 G2 oracle 冲突，按 §V2
    # 点名修正为 oracle 形状——非为过而改。）
    assert "\n\nfoo\n\n" in html, html
