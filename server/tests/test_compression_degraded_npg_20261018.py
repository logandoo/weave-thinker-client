"""Compression C2: NPG skeleton + degraded fallback + telemetry (2026-10-18)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.context_compressor import ContextCompressor, estimate_tokens_rough


class NoSummaryCompressor(ContextCompressor):
    async def _generate_summary_async(self, turns, focus_topic=None, delivery_context=""):
        return None


def _msg(role, content, **kw):
    out = {"role": role, "content": content}
    out.update(kw)
    return out


def _history_with_tool(user_text, code, output):
    return [
        _msg("user", user_text),
        _msg("assistant", "执行", tool_calls=[{
            "id": "c1", "type": "function",
            "function": {"name": "execute_code", "arguments": '{"code": "%s"}' % code},
        }]),
        _msg("tool", output, tool_call_id="c1", name="execute_code"),
        _msg("assistant", "好"),
        _msg("user", "继续"),
    ]


def test_npg_skeleton_keeps_trusted_receipt():
    from app.services.context_compressor import _npg_evidence_skeletons
    msgs = _history_with_tool(
        "总预算是 20000，帮我算双倍", "size = 20000*2; print(size)", "40000"
    )
    sk = _npg_evidence_skeletons(msgs)
    assert sk.get("c1") == [40000.0]


def test_npg_skeleton_drops_identifier_number():
    from app.services.context_compressor import _npg_evidence_skeletons
    msgs = _history_with_tool("车次 D2281 到了吗", "print(len(rows))", "2281")
    assert _npg_evidence_skeletons(msgs) == {}


def test_summary_failure_degrades_instead_of_returning_input():
    c = NoSummaryCompressor(quiet=True)
    c.update_context_length(20000)
    c.protect_first_n = 1
    c.protect_last_tokens = 2000
    msgs = [_msg("user", "原始请求")]
    for _ in range(8):
        msgs.append(_msg("assistant", "a" * 20000))
        msgs.append(_msg("user", "继续"))
    before = estimate_tokens_rough(msgs)
    out = asyncio.run(c.compress_async(msgs))
    after = estimate_tokens_rough(out)
    assert c.last_degraded is True
    assert after < before
    assert c.consume_events()[-1]["degraded"] is True


def test_ineffective_forces_degraded_instead_of_skip():
    c = ContextCompressor(quiet=True)
    c.update_context_length(10000)
    c.threshold_tokens = 1000
    c._ineffective_count = 2
    msgs = [_msg("user", "x" * 5000), _msg("assistant", "y" * 5000)]
    c._last_ineffective_msg_count = len(msgs)
    c._last_ineffective_tokens = estimate_tokens_rough(msgs)
    assert c.should_compress(msgs, prompt_tokens=5000) is True
    assert c._emergency_degraded is True


def test_recovery_index_has_ids_and_files():
    from app.services.context_compressor import _recovery_index_for
    idx = _recovery_index_for([
        {"role": "user", "id": "m1", "content": "改 src/main.py"},
        {"role": "assistant", "id": "m2", "content": "见 /tmp/out.txt"},
    ])
    assert "recovery_index" in idx
    assert "m1" in idx and "src/main.py" in idx and "/tmp/out.txt" in idx
    assert len(idx) <= 1024
