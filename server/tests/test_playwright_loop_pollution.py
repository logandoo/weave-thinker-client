"""RED tests — playwright sync API running-loop 泄漏根治（2026-10-04 全量 278 红事故）。

机制：playwright.sync_api 用 greenlet 在**调用线程**内驱动私有 event loop；用完
该线程 `asyncio._get_running_loop()` 残留 running loop → 同线程其后任何
`asyncio.run()` 报 `asyncio.run() cannot be called from a running event loop`。
一次导出/数学渲染就毒死整个 pytest 进程（npg/numeric/office/voice/workspace 全灭）。

修复契约：所有 sync playwright 工作在唯一专用工作线程执行（该线程永不
asyncio.run）——调用线程零污染；浏览器缓存随之单线程复用。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_playwright_loop_pollution.py -v
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _assert_loop_clean(label: str):
    loop = asyncio._get_running_loop()
    assert loop is None, f"{label}: 调用线程残留 running loop {loop!r}"
    asyncio.run(asyncio.sleep(0))  # 不抛才干净


def test_math_render_leaves_caller_thread_clean():
    from app.api.notes import _markdown_to_html_with_mermaid
    _assert_loop_clean("pre")
    html = _markdown_to_html_with_mermaid('\n\\(x^2\\)\n\n')
    assert html and ('math-inline' in html or 'svg' in html), html[:200]
    _assert_loop_clean("after math render")


def test_mermaid_render_leaves_caller_thread_clean():
    from app.api.notes import _markdown_to_html_with_mermaid
    html = _markdown_to_html_with_mermaid('\n```mermaid\ngraph TD; A-->B;\n```\n')
    assert html
    _assert_loop_clean("after mermaid render")


def test_echarts_render_leaves_caller_thread_clean():
    from app.api.notes import _markdown_to_html_with_mermaid
    html = _markdown_to_html_with_mermaid(
        '\n```echarts\n{"option": {"xAxis": {"type": "category", "data": ["a"]}, '
        '"yAxis": {}, "series": [{"type": "bar", "data": [1]}]}}\n```\n')
    assert html
    _assert_loop_clean("after echarts render")


def test_visual_critic_screenshot_leaves_caller_thread_clean():
    from app.services.visual_critic import _screenshot_html
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write("<html><body><h1>t</h1></body></html>")
        path = f.name
    try:
        shot = _screenshot_html(path)
        assert shot is not None and len(shot) > 0, "headless 截图必须真出图（A4.9 wave2：允许失败曾掩盖池化/with 会话互斥回归）"
    finally:
        os.unlink(path)
    _assert_loop_clean("after visual_critic screenshot")


def test_all_playwright_sites_decorated():
    """A4.9 wave2：五个 sync_playwright 站点必须全部挂线程隔离（在位钉），
    且 with-block 站点走 ephemeral（池化会话与 with 会话不得同线程）。"""
    from app.api.notes import (
        _batch_render_echarts_svg, _batch_render_mermaid_and_math,
        _batch_render_math_with_mathjax, _render_math_with_mathjax,
    )
    from app.services.visual_critic import _screenshot_html
    thread_sites = [_batch_render_echarts_svg, _batch_render_mermaid_and_math,
                    _batch_render_math_with_mathjax]
    ephemeral_sites = [_render_math_with_mathjax, _screenshot_html]
    for fn in thread_sites:
        assert getattr(fn, "_pw_kind", None) == "thread", f"{fn.__name__} 未挂 @pw_thread"
    for fn in ephemeral_sites:
        assert getattr(fn, "_pw_kind", None) == "ephemeral", f"{fn.__name__} 未挂 @pw_ephemeral"


def test_mixed_sessions_do_not_poison():
    """池化会话（批渲染）与 with-block 会话（mathjax/截图）先后执行互不毒化
    ——A4.9 wave2 Critical 回归钉（此前同线程混用报 Sync API inside asyncio loop）。"""
    from app.api.notes import _markdown_to_html_with_mermaid
    from app.services.visual_critic import _screenshot_html
    html = _markdown_to_html_with_mermaid('\n```mermaid\ngraph TD; A-->B;\n```\n')
    assert html
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write("<html><body>x</body></html>")
        path = f.name
    try:
        shot = _screenshot_html(path)
        assert shot is not None
    finally:
        os.unlink(path)
    html2 = _markdown_to_html_with_mermaid('\n\\(x^2\\)\n\n')
    assert html2
    _assert_loop_clean("after mixed pool+with sessions")
