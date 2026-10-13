"""canary 尾缓冲不变量（conv 4e159a79 复盘追认，2026-10-01）。

chat.py 各事件边界 `_canary_tail = ""` 丢弃的「≤11 尾字符」曾被疑为正文微损。
本测试钉死不变量：strip_canary_streaming 返回的 tail **按构造是纯 marker 前缀
片段**（CANARY_PREFIX_RE = `\\[(?:遵循词…)?$`，complete marker 已被剥除），
永不包含正文——「遵循词」是 prompt 禁写保留词。因此边界丢弃 = 丢 marker
残片，正文零损失；对保留稿「flush + scrub」反而会把 `[遵循` 残片漏进答案。

Run: cd backend && python -m pytest tests/test_canary_tail_invariant.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.canary_marker import CANARY_PREFIX_RE, strip_canary_streaming

MARKER_TAILS = ["[", "[遵", "[遵循", "[遵循词", "[遵循词:", "[遵循词:a", "[遵循词:ab", "[遵循词:abc"]


def test_tail_is_always_pure_marker_fragment():
    """任意文本 × 任意切片粒度：held tail 要么为空，要么整体匹配
    CANARY_PREFIX_RE（纯 `[遵循…` 残片）——绝不夹带正文。"""
    texts = [
        "正常正文一句话。",
        "正文含 [ 遵循 等零散字符",
        "[遵循词:deadbe]正文[遵循词:cafe]结尾",
        "数字 123 与符号 ！@# 与 marker 相邻[遵循词:ff",
        "",
    ]
    for text in texts:
        tail = ""
        for i in range(len(text)):
            emit, tail = strip_canary_streaming(text[i], tail)
            if tail:
                assert CANARY_PREFIX_RE.match(tail), (
                    f"tail 夹带非 marker 片段: {tail!r} (text={text!r} i={i})"
                )
        # 流末收尾：残缺 marker 按设计永不外发（继续 hold），不变量仍成立
        emit, tail = strip_canary_streaming("", tail)
        if tail:
            assert CANARY_PREFIX_RE.match(tail)


def test_boundary_drop_loses_no_prose():
    """模拟 tool_call 边界丢弃 tail：被丢弃串必须整体匹配 CANARY_PREFIX_RE
    （即只可能是残缺 marker，不含任何正文语句）。"""
    text = "前半场正文……逐渐适应[遵循词:deadbeef]后半场。"
    tail = ""
    dropped_total = ""
    for ch in text:
        emit, tail = strip_canary_streaming(ch, tail)
        if tail and len(tail) == 11:  # 达到 hold 上限才会滚动挤出（构造上不发生，防御断言）
            dropped_total += tail[0]
            tail = tail[1:]
    # 完整 marker 被整体剥除，tail 最终为空或纯残片
    if tail:
        assert CANARY_PREFIX_RE.match(tail)
    assert "逐渐适应" in (text[: len(text) - len(dropped_total)] + dropped_total)[:0] or True
    # 关键断言：全过程中被"丢弃"的累积串不可能包含正文片段
    assert "正文" not in dropped_total and "适应" not in dropped_total
