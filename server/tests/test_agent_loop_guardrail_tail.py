"""W12 P1#9：收尾门禁抽取（direct_reply 快捷路径共用）— A4.8 test-first。"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import hook_service as hs  # noqa: E402
from app.services.agent_loop import AgentLoop  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    hs.clear_hooks()
    yield
    hs.clear_hooks()


def _mk_state(**kw):
    st = SimpleNamespace(
        loop_guard_feedback="",
        stop_hook_blocks=0,
        iterations=1,
        messages=[],
        turn_content_segments=[],
        session_id="s1",
    )
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def _mk_loop(ws="/tmp/ws"):
    return AgentLoop(llm=SimpleNamespace(), tool_schemas=[], workspace_path=ws)


def test_tail_allows_without_hooks():
    loop = _mk_loop()
    events, blocked = asyncio.run(loop._completion_guardrails_tail(
        _mk_state(), SimpleNamespace(id="c1"), SimpleNamespace(id="u1")
    ))
    assert blocked is False and events == []


def test_tail_blocks_and_injects_stop_feedback():
    async def blocker(ctx):
        assert ctx.workspace_path == "/tmp/ws"
        return hs.StopDecision(allow=False, feedback="fix evidence", reason="gate red at stop")

    hs.register_stop_hook(blocker)
    loop = _mk_loop()
    state = _mk_state()
    events, blocked = asyncio.run(loop._completion_guardrails_tail(
        state, SimpleNamespace(id="c1"), SimpleNamespace(id="u1"), "final answer"
    ))
    assert blocked is True
    assert state.stop_hook_blocks == 1
    assert events and events[0]["agent_step"]["name"] == "stop_hook"
    assert state.messages and "fix evidence" in str(state.messages[-1])


def test_tail_loop_guard_feedback_first_and_consumed():
    loop = _mk_loop()
    state = _mk_state(loop_guard_feedback="correct it")
    events, blocked = asyncio.run(loop._completion_guardrails_tail(
        state, SimpleNamespace(id="c1"), SimpleNamespace(id="u1")
    ))
    assert blocked is True
    assert events[0]["agent_step"]["name"] == "loop_guard"
    assert state.loop_guard_feedback == ""
    assert state.messages and "correct it" in str(state.messages[-1])


def test_tail_loop_guard_empty_content_no_empty_draft():
    loop = _mk_loop()
    state = _mk_state(loop_guard_feedback="correct it")
    events, blocked = asyncio.run(loop._completion_guardrails_tail(
        state, SimpleNamespace(id="c1"), SimpleNamespace(id="u1"), ""
    ))
    assert blocked is True
    assert events[0]["agent_step"]["name"] == "loop_guard"
    assert not any(m.get("role") == "assistant" and m.get("content") == "" for m in state.messages)


def test_direct_reply_branch_wiring_pin():
    """复审 A-I2/B-I3 接线钉（wiring pin，行为面由本文件前四腿承担）：
    direct_reply 分支必须 ①累积 content+reasoning ②传入收尾门禁（含 reasoning）
    ③被拦时先 emit audit_reset 再落主循环（否则客户端叠影双答，conv efaf8f9c 类）。
    """
    import re
    src = open("app/services/agent_loop.py", encoding="utf-8").read()
    anchor = src.find('coord_result.get("route") == "direct_reply"')
    assert anchor > -1, "direct_reply 分支未找到"
    block = src[anchor:anchor + 6000]
    assert "_completion_guardrails_tail(" in block, "direct_reply 分支未接线收尾门禁"
    assert re.search(r'_dr_reasoning\s*\+=', block), "分支未累积 reasoning_content"
    tail_pos = block.index("_completion_guardrails_tail(")
    tail_call = block[tail_pos:tail_pos + 400]
    assert "_dr_reasoning" in tail_call, "收尾门禁调用未传 reasoning"
    assert '{"audit_reset": True}' in block, "被拦落主循环前缺 audit_reset（客户端将叠影双答）"
    # audit_reset 必须先于门禁事件、在落主循环之前发出
    assert block.index('{"audit_reset": True}') < block.index("if not _g_blocked:"), \
        "audit_reset 必须在最终 return 判定之前"
