"""RED tests — context compression bounds + fidelity (2026-10-18).

Production evidence (conv dca19082, 60 box 2026-10-17):
  - Spike guard repeatedly compressed when the provider-measured prompt was
    far below the window (estimate 316k vs measured 120k; estimate 708k vs
    measured 143k).
  - One compression left 1,044,453 -> 322,811 tokens against a 262k window
    (known F9 defect: protected head/tail have no single-message cap).
  - `_serialize_turns` truncated every message at 3,000 chars and dropped
    tool arguments, so the summary could not carry exact paths/commands.

Contracts:
  1. `should_compress(messages, prompt_tokens=measured)` uses the
     provider-measured value, not the inflated rough estimate.
  2. Compression output is bounded: an oversized message in the protected
     head or tail moves into the summarized middle.
  3. The serializer keeps head+tail of long tool output and includes tool
     arguments (paths/commands are the facts a resumed agent needs).
  4. The summary prompt forbids importing facts from other conversations or
     global memory (cross-conversation contamination guard).

Run: cd backend && python -m pytest tests/test_context_compressor_bounds_20261018.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.context_compressor import (
    ContextCompressor,
    build_summary_prompt,
    estimate_text_tokens_rough,
    estimate_tokens_rough,
)


class FakeSummaryCompressor(ContextCompressor):
    last_turns = None

    async def _generate_summary_async(self, turns, focus_topic=None, delivery_context=""):
        self.last_turns = list(turns)
        return "[CONTEXT COMPACTION — REFERENCE ONLY]\n" + ("summary " * 400)


def _msg(role, content, **kw):
    out = {"role": role, "content": content}
    out.update(kw)
    return out


def _small_history(n=8):
    msgs = [_msg("user", "原始请求：" + "需求" * 20)]
    for i in range(n):
        msgs.append(_msg("assistant", f"第 {i} 轮回答"))
        msgs.append(_msg("user", f"第 {i} 轮补充"))
    return msgs


def test_should_compress_uses_measured_prompt_tokens():
    c = FakeSummaryCompressor(quiet=True)
    c.update_context_length(1_000_000)
    c.threshold_percent = 0.65
    # Rough estimate is deliberately huge (CJK/tool JSON inflation).
    msgs = _small_history(40)
    msgs.append(_msg("tool", "x" * 3_000_000))
    assert estimate_tokens_rough(msgs) > c.threshold_tokens
    # Provider measured 120k of a 1M window: must NOT compress.
    assert c.should_compress(msgs, prompt_tokens=120_000) is False


def test_oversized_tail_message_is_summarized_not_protected():
    c = FakeSummaryCompressor(quiet=True)
    c.update_context_length(100_000)
    c.threshold_percent = 0.8
    c.protect_last_tokens = 20_000
    c.protect_first_n = 1
    msgs = _small_history(10)
    # A single giant assistant reasoning block sits in the last three messages
    # (the 2026-10-17 production shape: 121k-char reasoning, no tool result).
    msgs.append(_msg("assistant", "调用工具", reasoning_content="R" * 500_000))
    msgs.append(_msg("user", "继续"))
    msgs.append(_msg("assistant", "最终回答"))
    before = estimate_tokens_rough(msgs)
    out = __import__("asyncio").run(c.compress_async(msgs))
    after = estimate_tokens_rough(out)
    assert before > 100_000
    assert after < int(100_000 * 0.8), (before, after, len(out))


def test_oversized_final_tool_message_is_summarized():
    """Compression runs BEFORE an iteration: the giant tool result appended by
    the previous iteration is the FINAL message. The old `i < n-1` exemption
    kept it verbatim; the first fix replaced it with a placeholder before the
    summary saw it (re-review Critical). It must reach the summary prompt with
    its tail evidence intact."""
    c = FakeSummaryCompressor(quiet=True)
    c.update_context_length(100_000)
    c.threshold_percent = 0.8
    c.protect_last_tokens = 20_000
    c.protect_first_n = 1
    msgs = _small_history(10)
    msgs.append(_msg("assistant", "调用工具", tool_calls=[{
        "id": "c1", "type": "function",
        "function": {"name": "terminal", "arguments": '{"command": "git status"}'},
    }]))
    msgs.append(_msg(
        "tool", "T" * 400_000 + "\nTAIL-EVIDENCE-MARKER", tool_call_id="c1"))
    before = estimate_tokens_rough(msgs)
    out = __import__("asyncio").run(c.compress_async(msgs))
    after = estimate_tokens_rough(out)
    assert before > 100_000
    assert after < int(100_000 * 0.8), (before, after, len(out))
    serialized = c._serialize_turns(c.last_turns or [])
    assert "TAIL-EVIDENCE-MARKER" in serialized, "final tool evidence lost before summary"
    assert "git status" in serialized, "tool arguments lost before summary"


def test_oversized_head_message_is_summarized_not_protected():
    c = FakeSummaryCompressor(quiet=True)
    c.update_context_length(100_000)
    c.threshold_percent = 0.8
    c.protect_last_tokens = 20_000
    c.protect_first_n = 3
    msgs = [_msg("user", "原始请求")]
    # The first assistant turn carries a giant reasoning block + args.
    msgs.append(_msg("assistant", "读取日志", reasoning_content="R" * 500_000))
    msgs.append(_msg("tool", "small", tool_call_id="c1"))
    msgs.extend(_small_history(10)[1:])
    before = estimate_tokens_rough(msgs)
    out = __import__("asyncio").run(c.compress_async(msgs))
    after = estimate_tokens_rough(out)
    assert before > 100_000
    assert after < int(100_000 * 0.8), (before, after, len(out))


def test_serializer_keeps_tail_and_tool_arguments():
    c = FakeSummaryCompressor(quiet=True)
    long = "HEAD-MARKER " + ("中间 " * 5000) + " TAIL-MARKER"
    turns = [
        _msg("user", "请检查 /home/user/Documents/DEV/chatbot"),
        _msg(
            "assistant",
            "执行命令",
            tool_calls=[{
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "cd /home/user/Documents/DEV/chatbot && git status"}',
                },
            }],
        ),
        _msg("tool", long, tool_call_id="c1"),
    ]
    serialized = c._serialize_turns(turns)
    assert "HEAD-MARKER" in serialized
    assert "TAIL-MARKER" in serialized, "long tool output lost its tail"
    assert "git status" in serialized, "tool arguments were dropped"
    assert "/home/user/Documents/DEV/chatbot" in serialized


def test_summary_prompt_has_conversation_boundary():
    prompt = build_summary_prompt(
        serialized="[USER]: a\n[ASSISTANT]: b",
        previous_summary=None,
        summary_budget=1200,
    )
    assert "其他会话" in prompt or "other conversation" in prompt.lower()
    assert "记忆" in prompt or "memory" in prompt.lower()
    assert "不得" in prompt or "do not" in prompt.lower()


def test_summary_budget_scales_above_old_ceiling():
    c = FakeSummaryCompressor(quiet=True)
    c.update_context_length(1_000_000)
    # The old ceiling was 6,000 tokens regardless of a 1M window.
    assert c._summary_budget_for(600_000) > 6_000
