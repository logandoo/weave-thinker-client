"""RED tests (A4.8) — 已流式展示的答案文本不得在工具调用边界被清空（conv 4e159a79）。

生产事故（60 箱 conv 4e159a79 末轮，2026-10-01）：
  creative 轮 coordinator 未预测工具 → PreToolGate 未武装 → 模型把修订设定
  前半场（一~六块）作为 content 直接流给用户；随后自发调 4 个 calculate
  核年份。chat.py tool_call 边界判定 `if not _pre_tool_gate.cap_released:
  assistant_content = ""` —— cap_released 只在 hold-cap 释放一条路径置 True，
  未武装直发流永远是 False → 前半场整段清空，落库只剩尾段
  「的## 七、势力组织（续）…」，done 刷新后用户已见的 1-6 块被替换删除。

契约（A4.9 I2 泛化）：
  A 工具调用边界只允许丢弃「从未推送给客户端」的预工具散文（gate.held 里的
    hold）；任何已流式送达用户的文本必须进入落库答案。
  B 原契约不回归：armed 且 tool 调用在 hold 窗口内到达（从未展示）→ 仍丢弃。
  C 截断续写（finish_reason=length → iteration_done(0) → 续写轮调工具）
    场景下 head+tail 都保留。

Run: cd backend && python -m pytest tests/test_pre_tool_streamed_prose_survives.py -v
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pre_tool_gate import PreToolGate

_CHAT_SRC = open(
    os.path.join(os.path.dirname(__file__), "..", "app", "api", "chat.py"),
    encoding="utf-8",
).read()

_TOOL_BLOCK_RE = re.compile(
    r'elif "tool_call" in event:(.*?)(?=\n                            elif ")',
    re.DOTALL,
)
_ITER_BLOCK_RE = re.compile(
    r'elif "iteration_done" in event:(.*?)(?=\n                            elif ")',
    re.DOTALL,
)


def _drive_streamed_turn(gate: PreToolGate, chunks):
    """复刻 relay 累加器语义：assistant_content += chunk；gate.on_content 决定
    是否直发。返回 (assistant_content, streamed_text)。"""
    assistant_content = ""
    streamed = ""
    for chunk, now in chunks:
        assistant_content += chunk
        out = gate.on_content(chunk, now)
        streamed += out
    return assistant_content, streamed


def test_never_armed_streamed_prose_survives_tool_call():
    """A：coordinator 未预测工具（creative 轮）→ gate 未武装 → 散文直发；
    自发调工具时边界不得清空已展示文本。"""
    from app.api.chat import _drop_unshown_pre_tool_prose

    gate = PreToolGate()
    gate.arm("direct_answer", False)  # 未武装
    assistant_content, streamed = _drive_streamed_turn(
        gate, [("## 一、世界总览\n\n……", 0.0), ("## 六、势力组织\n\n……", 1.0)]
    )
    assert streamed  # 用户看到了
    kept = _drop_unshown_pre_tool_prose(assistant_content, gate)
    assert "## 一、世界总览" in kept


def test_unstreamed_held_prose_still_dropped():
    """B：armed 且工具调用在 hold 窗口内到达（从未展示）→ 仍丢弃（原契约）。"""
    from app.api.chat import _drop_unshown_pre_tool_prose

    gate = PreToolGate()
    gate.arm("tool_loop", True)
    assistant_content, streamed = _drive_streamed_turn(
        gate, [("让我先检索一下。", 0.0), ("检索进行中……", 1.0)]
    )
    assert not streamed  # 从未展示
    kept = _drop_unshown_pre_tool_prose(assistant_content, gate)
    assert kept == ""


def test_cap_released_prose_survives_second_tool_call():
    """A：hold-cap 释放后的散文，在第二个工具边界不得因 cap_released 被
    _clear() 复位而清空。"""
    from app.api.chat import _drop_unshown_pre_tool_prose

    gate = PreToolGate()
    gate.arm("tool_loop", True)
    assistant_content, streamed = _drive_streamed_turn(
        gate, [("第一段正文。", 1.0), ("第二段正文。", 10.0)]
    )
    assert streamed  # hold-cap 6s 释放
    gate.on_tool_call()  # 第一个工具边界 → _clear() 复位 cap_released
    assert gate.cap_released is False
    # 后续散文直发（已 disarm），再遇工具边界：
    assistant_content += "第三段。"
    gate.on_content("第三段。", 8.0)
    kept = _drop_unshown_pre_tool_prose(assistant_content, gate)
    assert "第一段正文" in kept and "第三段" in kept


def test_truncation_continuation_keeps_head_and_tail():
    """C：截断续写全流程——head 直发 → iteration_done(0)（截断边界）→
    续写轮工具调用 → tail。落库必须包含 head 与 tail。"""
    from app.api.chat import _drop_unshown_pre_tool_prose

    gate = PreToolGate()
    gate.arm("direct_answer", False)
    assistant_content = ""
    # iter1：sections 1-6 直发（截断于句中"的"）
    for chunk, now in [("一、二、三、四、五、六块正文……逐渐适应", 0.0)]:
        assistant_content += chunk
        streamed = gate.on_content(chunk, now)
        assert streamed
    # 截断边界：iteration_done(0)（gate 未武装 → 无 hold 可释放）
    assert gate.on_iteration_done(0, 5.0) == ""
    # iter2：工具调用（calculate 核年份）——旧代码在此清空 head
    assistant_content = _drop_unshown_pre_tool_prose(assistant_content, gate)
    gate.on_tool_call()
    # iter2 续写 tail
    assistant_content += "的## 七、势力组织（续）\n\n十一、评价"
    assert "一、二、三、四、五、六块正文" in assistant_content
    assert "的## 七、势力组织（续）" in assistant_content


def test_iteration_done_tool_drop_keeps_streamed():
    """A：iteration_done(tool_calls>0) 安全网同口径——只丢未展示 hold。"""
    from app.api.chat import _drop_unshown_pre_tool_prose

    gate = PreToolGate()
    gate.arm("tool_loop", True)
    assistant_content, streamed = _drive_streamed_turn(gate, [("已展示正文。", 1.0), ("（续）", 10.0)])
    assert streamed  # cap 释放
    kept = _drop_unshown_pre_tool_prose(assistant_content, gate)
    assert kept == "已展示正文。（续）"


def test_tool_call_block_uses_helper_not_bare_wipe():
    """接线契约：tool_call 边界调用 helper，不再按 cap_released 整段清空。"""
    m = _TOOL_BLOCK_RE.search(_CHAT_SRC)
    assert m, "tool_call 事件处理块不存在"
    blk = m.group(1)
    assert "_drop_unshown_pre_tool_prose" in blk
    assert 'assistant_content = ""' not in blk


def test_iteration_done_block_trims_unstreamed():
    """接线契约：iteration_done(tool_calls>0) 同口径修剪未展示 hold。"""
    m = _ITER_BLOCK_RE.search(_CHAT_SRC)
    assert m, "iteration_done 事件处理块不存在"
    blk = m.group(1)
    assert "_drop_unshown_pre_tool_prose" in blk
