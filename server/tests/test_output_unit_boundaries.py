"""输出单元契约（用户裁定 2026-10-14，取代 conv 4e159a79 波的「已展示一律保留」）：

  「只有在真正是长输出被截断时才拼接，不然就是正常的多轮次输出。」

新契约：
  A 非拼接链（stitch_active=False）：工具调用边界处的预工具散文是**独立输出
    单元**——已展示部分停车入 display_sequence 时间线（过程区），未展示 hold
    仍丢弃（conv 149ce886 占位/复述散文契约不变）；**不得**拼进终局答案。
  B 拼接链（stitch_active=True，唯一合法拼接=finish_reason=length 续写）：
    链上文本全量保留于终局答案（conv 4e159a79 小说 1-6 块不得再现蒸发）。
  C 代际边界必清累积：search_demand / tool_demand / loop_guard / 错误重试 /
    截断工具重问 的 continue 前必须 yield audit_reset（与审计打回同口径），
    否则旧稿+新稿并存=「两遍几乎一致」（60 conv 90723ff0 / 本地 conv d6ba1860）。

Run: cd backend && python -m pytest tests/test_output_unit_boundaries.py -v
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pre_tool_gate import PreToolGate

# C-02 move-only: the SSE event handling now lives in the transport module.
_CHAT_SRC = open(
    os.path.join(os.path.dirname(__file__), "..", "app", "api",
                 "chat_stream_transport.py"),
    encoding="utf-8",
).read()
_LOOP_SRC = "".join(
    open(
        os.path.join(os.path.dirname(__file__), "..", "app", "services", name),
        encoding="utf-8",
    ).read()
    for name in ("agent_loop.py", "agent_loop_orchestrator.py")
)

_TOOL_BLOCK_RE = re.compile(
    r'elif "tool_call" in event:(.*?)(?=\n\s+elif ")',
    re.DOTALL,
)
_ITER_BLOCK_RE = re.compile(
    r'elif "iteration_done" in event:(.*?)(?=\n\s+elif ")',
    re.DOTALL,
)


def _drive(gate: PreToolGate, chunks):
    assistant_content = ""
    streamed = ""
    for chunk, now in chunks:
        assistant_content += chunk
        streamed += gate.on_content(chunk, now)
    return assistant_content, streamed


def test_non_stitch_tool_boundary_parks_shown_prose():
    """A：已展示预工具散文在工具边界停车入时间线，不进终局答案。"""
    from app.api.chat import _split_pre_tool_prose

    gate = PreToolGate()
    gate.arm("direct_answer", False)  # 未武装 → 直发（用户已见）
    content, streamed = _drive(gate, [("我先看看这两个引擎的文档。", 0.0)])
    assert streamed
    kept, parked = _split_pre_tool_prose(content, gate, stitch_active=False)
    assert kept == ""
    assert "我先看看" in parked


def test_unshown_hold_still_dropped_not_parked():
    """A：从未展示的 hold 仍丢弃（conv 149ce886 契约不变），不停车、不进答案。"""
    from app.api.chat import _split_pre_tool_prose

    gate = PreToolGate()
    gate.arm("tool_loop", True)
    content, streamed = _drive(gate, [("让我先检索一下。", 0.0), ("检索中……", 1.0)])
    assert not streamed
    kept, parked = _split_pre_tool_prose(content, gate, stitch_active=False)
    assert kept == ""
    assert parked == ""


def test_stitch_chain_keeps_head_and_tail():
    """B：截断续写链（唯一合法拼接）——工具边界不得清空链上文本。"""
    from app.api.chat import _split_pre_tool_prose

    gate = PreToolGate()
    gate.arm("direct_answer", False)
    content, streamed = _drive(gate, [("一、二、三、四、五、六块正文……的", 0.0)])
    assert streamed
    # 截断边界 → stitch_active=True；续写轮调 calculate
    kept, parked = _split_pre_tool_prose(content, gate, stitch_active=True)
    assert "一、二、三、四、五、六块正文" in kept
    assert parked == ""
    kept += "## 七、势力组织（续）"
    assert "六块正文" in kept and "七、势力组织" in kept


def test_cap_released_second_boundary_parks():
    """A：hold-cap 释放后的散文，第二个工具边界同样停车（不拼进终局）。"""
    from app.api.chat import _split_pre_tool_prose

    gate = PreToolGate()
    gate.arm("tool_loop", True)
    content, streamed = _drive(gate, [("第一段正文。", 1.0), ("第二段正文。", 10.0)])
    assert streamed
    gate.on_tool_call()
    content += "第三段。"
    gate.on_content("第三段。", 8.0)
    kept, parked = _split_pre_tool_prose(content, gate, stitch_active=False)
    assert kept == ""
    assert "第一段正文" in parked and "第三段" in parked


def test_chat_tool_block_wires_splitter():
    """接线：tool_call 边界走 _split_pre_tool_prose 并把 parked 写进 display_sequence。"""
    m = _TOOL_BLOCK_RE.search(_CHAT_SRC)
    assert m, "tool_call 事件处理块不存在"
    blk = m.group(1)
    assert "_split_pre_tool_prose" in blk
    assert "display_sequence.append" in blk
    assert 'assistant_content = ""' not in blk


def test_chat_iteration_block_wires_splitter():
    """接线：iteration_done(tool_calls>0) 安全网同口径走 splitter。"""
    m = _ITER_BLOCK_RE.search(_CHAT_SRC)
    assert m, "iteration_done 事件处理块不存在"
    blk = m.group(1)
    assert "_split_pre_tool_prose" in blk


def test_chat_tracks_stitch_state():
    """接线：chat.py 持有 _stitch_active；iteration_done 的 stitch 标记置位；
    audit_reset 复位。"""
    # C-02c: the per-run accumulators live on `_AgentLoopRun` (run.<field>).
    # Pin the SET site (not just the field name, which also appears in the
    # dataclass declaration) so deleting the wiring still fails this test.
    assert "run.stitch_active = True" in _CHAT_SRC
    m = _ITER_BLOCK_RE.search(_CHAT_SRC)
    assert m and "stitch" in m.group(1)
    ar = re.search(r'elif "audit_reset" in event:(.*?)(?=\n\s+elif ")', _CHAT_SRC, re.DOTALL)
    assert ar and "run.stitch_active = False" in ar.group(1)


def test_loop_truncation_branch_signals_stitch():
    """接线：finish_reason=length 无工具截断分支的 iteration_done 携带 stitch 标记。"""
    trunc = re.search(
        r'if finish_reason == "length":(.*?)# Extract any DeepSeek-DSML',
        _LOOP_SRC, re.DOTALL,
    )
    assert trunc, "主环截断分支不存在"
    blk = trunc.group(1)
    assert '"stitch": True' in blk


def test_loop_generation_boundaries_yield_reset():
    """C：五个代际边界 continue 前必须发 audit_reset（旧稿+新稿并存=双倍输出）。"""
    checks = {
        "search_demand": r'state\.search_demanded = True(.*?)continue',
        "tool_demand": r'state\.tool_demanded = True(.*?)continue',
    }
    for name, pat in checks.items():
        m = re.search(pat, _LOOP_SRC, re.DOTALL)
        assert m, f"{name} 分支不存在"
        assert "audit_reset" in m.group(1), f"{name} 的 continue 前缺少 audit_reset 事件（双倍输出源）"
    # loop_guard 两处（主循环迭代边界 + 收尾门禁）都必须发 reset
    lg_sites = re.findall(
        r'if state\.loop_guard_feedback.*?(?:continue|return events, True)',
        _LOOP_SRC, re.DOTALL,
    )
    assert len(lg_sites) >= 2, f"loop_guard 打回点应有 2 处（主循环+收尾门禁），实得 {len(lg_sites)}"
    for i, blk in enumerate(lg_sites):
        assert "audit_reset" in blk, f"loop_guard 打回点 #{i+1} 缺少 audit_reset（双倍输出源）"
    # 截断工具重问（所有工具调用被丢弃→请重新调用）同样需要 reset
    reask = re.search(
        r'if not tool_calls_collected:\s*logger\.warning\(\s*"All tool calls dropped due to truncation(.*?)continue',
        _LOOP_SRC, re.DOTALL,
    )
    assert reask, "截断工具重问分支不存在"
    assert "audit_reset" in reask.group(1), "截断工具重问 continue 前缺少 audit_reset"


def test_parked_text_precedes_tool_placeholder():
    """C1（A4.9）：parked 文本必须 append 在 tool_placeholder **之前**——
    与 SSE 直播顺序一致，且 _display_sequence_keep_evidence 只丢「最后工具卡
    之后」的 text，parked 在前=压缩后存活（用户已见文本不得刷新即蒸发）。"""
    m = _TOOL_BLOCK_RE.search(_CHAT_SRC)
    assert m
    blk = m.group(1)
    parked_at = blk.find('_parked_prose')
    placeholder_at = blk.find('{"type": "tool_placeholder"}')
    assert parked_at != -1 and placeholder_at != -1
    assert parked_at < placeholder_at, "parked 文本必须在 tool_placeholder 之前（A4.9 C1）"


def test_stop_hook_boundary_resets():
    """I1（A4.9）：stop_hook 收尾门禁打回分支必须发 audit_reset。"""
    m = re.search(
        r'(stop hook blocks completion.*?return events, True)',
        _LOOP_SRC, re.DOTALL,
    )
    assert m
    assert "audit_reset" in m.group(1), "stop_hook 打回缺 audit_reset（双倍输出源）"


def test_tool_loop_guard_boundary_parks():
    """I2（A4.9）：tool_loop_guard 强制终答边界发 iteration_done（停车口径，
    与「最大搜索次数」边界同构），否则散文拼进强制终答。"""
    m = re.search(
        r'("name": "tool_loop_guard".*?continue)',
        _LOOP_SRC, re.DOTALL,
    )
    assert m
    assert '"iteration_done"' in m.group(1), "tool_loop_guard 边界缺 iteration_done（过程输出并入终局源）"


def test_stitch_state_resets_on_compression_and_deathmatch():
    """I3（A4.9）：压缩与死磕轮间必须复位 _stitch_active/_stitch_base。"""
    comp = re.search(r'(elif "compression" in event:.*?)(?=\n\s+elif ")', _CHAT_SRC, re.DOTALL)
    assert comp and "run.stitch_active = False" in comp.group(1)
    dm = re.search(r'(# Reset accumulators for next turn.{0,1500})', _CHAT_SRC, re.DOTALL)
    assert dm and "run.stitch_active = False" in dm.group(1)


def test_vision_output_files_tenant_scoped():
    """I6（A4.9）：vision_interpret 的 output_files 放行必须按会话归属校验。"""
    import app.tools.vision_interpret as vi

    src = open(vi.__file__, encoding="utf-8").read()
    assert 'startswith(f"screenshot_{_conv_id}")' in src
    assert "_conv_id" in src
