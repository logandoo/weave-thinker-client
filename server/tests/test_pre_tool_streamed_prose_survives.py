"""conv 4e159a79 回归面 —— 按 2026-10-14 用户裁定的新输出单元契约重写。

旧契约（2026-10-01 波）：「已流式展示的文本一律保留进落库答案」。
新契约（用户裁定 2026-10-14）：
  「只有在真正是长输出被截断时才拼接，不然就是正常的多轮次输出。」
  → 非拼接链：预工具散文停车进时间线（不拼进终局答案）；
  → 拼接链（finish_reason=length 续写）：链上文本全量保留（4e159a79 的
    小说 1-6 块场景就是续写链，本文件 C 场景钉住不回归）。

test-change 依据：spec 变更（用户裁定）覆盖旧断言语义；场景（4e159a79
截断续写、conv 149ce886 未展示 hold 丢弃）保留，仅断言面随契约更新。
完整新契约面见 tests/test_output_unit_boundaries.py。

Run: cd backend && python -m pytest tests/test_pre_tool_streamed_prose_survives.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pre_tool_gate import PreToolGate


def _drive_streamed_turn(gate: PreToolGate, chunks):
    assistant_content = ""
    streamed = ""
    for chunk, now in chunks:
        assistant_content += chunk
        out = gate.on_content(chunk, now)
        streamed += out
    return assistant_content, streamed


def test_never_armed_streamed_prose_parks_not_merges():
    """4e159a79 场景 A（新契约）：未武装直发散文在工具边界**停车**进时间线
    （不蒸发、不拼进终局答案）。"""
    from app.api.chat import _split_pre_tool_prose

    gate = PreToolGate()
    gate.arm("direct_answer", False)
    assistant_content, streamed = _drive_streamed_turn(
        gate, [("## 一、世界总览\n\n……", 0.0), ("## 六、势力组织\n\n……", 1.0)]
    )
    assert streamed  # 用户看到了
    kept, parked = _split_pre_tool_prose(assistant_content, gate, stitch_active=False)
    assert kept == ""  # 不拼进终局
    assert "## 一、世界总览" in parked  # 停车保真（时间线可见）


def test_unstreamed_held_prose_still_dropped():
    """conv 149ce886 契约不变：armed 且工具调用在 hold 窗口内到达（从未展示）
    → 丢弃，不停车、不进答案。"""
    from app.api.chat import _split_pre_tool_prose

    gate = PreToolGate()
    gate.arm("tool_loop", True)
    assistant_content, streamed = _drive_streamed_turn(
        gate, [("让我先检索一下。", 0.0), ("检索进行中……", 1.0)]
    )
    assert not streamed
    kept, parked = _split_pre_tool_prose(assistant_content, gate, stitch_active=False)
    assert kept == "" and parked == ""


def test_truncation_continuation_keeps_head_and_tail():
    """4e159a79 核心回归（拼接链契约）：截断续写链上 head+tail 全量保留——
    finish_reason=length 续写是唯一合法拼接场景，工具边界不得清空链上文本。"""
    from app.api.chat import _split_pre_tool_prose

    gate = PreToolGate()
    gate.arm("direct_answer", False)
    assistant_content = ""
    for chunk, now in [("一、二、三、四、五、六块正文……逐渐适应", 0.0)]:
        assistant_content += chunk
        assert gate.on_content(chunk, now)
    # 截断边界：iteration_done(0, stitch=True) → 链激活
    assert gate.on_iteration_done(0, 5.0) == ""
    kept, parked = _split_pre_tool_prose(assistant_content, gate, stitch_active=True)
    assert "一、二、三、四、五、六块正文" in kept and parked == ""
    # 续写轮调 calculate（工具边界）→ 链上 head 不得清空
    kept, _ = _split_pre_tool_prose(kept, gate, stitch_active=True)
    kept += "的## 七、势力组织（续）\n\n十一、评价"
    assert "一、二、三、四、五、六块正文" in kept
    assert "的## 七、势力组织（续）" in kept


def test_cap_released_prose_parks_at_second_tool_call():
    """hold-cap 释放后的散文：第二个工具边界同样停车（新契约，不拼进终局）。"""
    from app.api.chat import _split_pre_tool_prose

    gate = PreToolGate()
    gate.arm("tool_loop", True)
    assistant_content, streamed = _drive_streamed_turn(
        gate, [("第一段正文。", 1.0), ("第二段正文。", 10.0)]
    )
    assert streamed
    gate.on_tool_call()
    assert gate.cap_released is False
    assistant_content += "第三段。"
    gate.on_content("第三段。", 8.0)
    kept, parked = _split_pre_tool_prose(assistant_content, gate, stitch_active=False)
    assert kept == ""
    assert "第一段正文" in parked and "第三段" in parked
