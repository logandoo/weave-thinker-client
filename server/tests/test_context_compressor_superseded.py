"""RED tests (A4.8) — 上下文压缩「失效信息删减」增强（2026-09-11）.

用户需求（T2）：压缩之外，上下文开始失效时应能删减无关信息（过期结果/
已解决的错误/被反转的结论）。本波落地两个保守机制（评估结论：不做独立
LLM validator 第二调用——压缩摘要本身即语义过滤器，且近满窗时额外调用
风险高；改为确定性 superseded 剪枝 + 摘要模板显式失效条款）：

  T1 `_prune_old_tool_results`：同一工具 + 同一参数的重复调用，较早的结果
     标记为 [Superseded ...]（保护尾区之外的旧结果）；最新一次保持原样。
  T2 负对照：参数不同的调用不被误标 superseded（仍走原有摘要化路径）。
  T3 `build_summary_prompt`：摘要提示词显式包含失效内容丢弃条款
     （已解决错误/被反转决定/被更新结果不得原样带入），且保留 focus 追加。

Run: cd backend && python -m pytest tests/test_context_compressor_superseded.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_messages():
    """Two identical execute_code calls + one long identical read + one
    different-arg read, followed by protected tail filler (last 3 untouched
    when protect_last_tokens=0)."""
    return [
        {"role": "user", "content": "start"},
        # A: identical pair #1 (short result)
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "execute_code", "arguments": '{"task": "run test"}'}}]},
        {"role": "tool", "tool_call_id": "call_a", "content": "FAILED: assertion xyz"},
        # B: identical pair #2 — latest occurrence stays authoritative
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_b", "type": "function",
             "function": {"name": "execute_code", "arguments": '{"task": "run test"}'}}]},
        {"role": "tool", "tool_call_id": "call_b", "content": "PASSED"},
        # D/E: identical long reads — earlier one superseded, not summarized
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_d", "type": "function",
             "function": {"name": "workspace_read", "arguments": '{"path": "a.txt"}'}}]},
        {"role": "tool", "tool_call_id": "call_d", "content": "x" * 300},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_e", "type": "function",
             "function": {"name": "workspace_read", "arguments": '{"path": "a.txt"}'}}]},
        {"role": "tool", "tool_call_id": "call_e", "content": "y" * 300},
        # F: different args — negative control (summarized, never superseded)
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_f", "type": "function",
             "function": {"name": "workspace_read", "arguments": '{"path": "b.txt"}'}}]},
        {"role": "tool", "tool_call_id": "call_f", "content": "z" * 300},
        # protected tail
        {"role": "user", "content": "trailing 1"},
        {"role": "assistant", "content": "trailing 2"},
        {"role": "user", "content": "trailing 3"},
    ]


def _pruned():
    from app.services.context_compressor import ContextCompressor
    comp = ContextCompressor(quiet=True, protect_last_tokens=0)
    out, pruned = comp._prune_old_tool_results(_make_messages())
    assert pruned > 0
    return {m.get("tool_call_id"): m.get("content", "") for m in out if m.get("role") == "tool"}


def test_t1_earlier_identical_call_superseded():
    contents = _pruned()
    assert contents["call_a"].startswith("[Superseded"), contents["call_a"][:120]
    assert contents["call_b"] == "PASSED", contents["call_b"]
    assert contents["call_d"].startswith("[Superseded"), contents["call_d"][:120]
    # latest occurrence is authoritative: never marked superseded (long results
    # still follow the pre-existing summarize-to-one-line pruning)
    assert not contents["call_e"].startswith("[Superseded"), contents["call_e"][:120]


def test_t2_different_args_not_superseded():
    contents = _pruned()
    assert not contents["call_f"].startswith("[Superseded")
    # existing behavior: long unique result summarized via _summarize_tool_result
    assert contents["call_f"].startswith("[workspace_read]"), contents["call_f"][:120]


def test_t3_summary_prompt_has_invalidation_rules():
    from app.services.context_compressor import build_summary_prompt

    prompt = build_summary_prompt(
        serialized="[USER]: hi",
        previous_summary=None,
        summary_budget=1000,
        focus_topic=None,
    )
    lowered = prompt.lower()
    # explicit discard rules: resolved errors / reversed decisions / superseded results
    assert "invalidat" in lowered or "superseded" in lowered or "失效" in prompt, prompt[:400]
    assert "resolved" in lowered or "已解决" in prompt, prompt[:400]
    assert "revers" in lowered or "反转" in prompt, prompt[:400]
    # existing contract preserved
    assert "TURNS TO SUMMARIZE" in prompt
    assert "## Active Task" in prompt

    focused = build_summary_prompt(
        serialized="[USER]: hi",
        previous_summary="PREV",
        summary_budget=1000,
        focus_topic="税负",
    )
    assert "FOCUS" in focused and "税负" in focused
    assert "PREV" in focused
