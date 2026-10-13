"""Audit evidence grounding for MCP tools (conv 6dcae019, 2026-09-03).

A RemPilot remote_shell (nvidia-smi over 8 GPUs) fed the whole answer, but
MCP tools were NOT in _AUDIT_GROUNDING_TOOLS — the auditor saw only a
head+tail fragment, declared 「nvidia-smi 输出被截断」 and needs_evidence'd
every draft until the budget fell to the selection fallback. MCP tool outputs
are the primary evidence of MCP-driven tasks: they must get the same
full-text (disk read-back) grounding as execute_code/terminal, with the
evidence token budget as the guardrail.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.agent_loop import _build_audit_evidence, _is_audit_grounding  # noqa: E402
from app.services.agent_loop import ToolCallResult  # noqa: E402


class _State:
    def __init__(self, tool_results):
        self.tool_results = tool_results
        self.messages = []


def test_mcp_tools_are_grounding_class():
    assert _is_audit_grounding("mcp_rempilot_rempilot_remote_shell_open")
    assert _is_audit_grounding("mcp_anyserver_any_tool")
    assert _is_audit_grounding("execute_code")
    assert not _is_audit_grounding("random_unknown_tool")


def test_mcp_result_middle_content_reaches_auditor_full_text():
    """The MIDDLE of a long MCP result must reach the auditor (full-text
    grounding), not just the head+tail fragment."""
    raw = (
        '{"success": true, "stdout": "HEAD LINE\\n'
        + "".join(f"GPU-{i}: 19767 MiB used, 32C, 15W\\n" for i in range(60))
        + 'MIDDLE_MARKER_19767MiB_TAIL_LINE"}'
    )
    tr = ToolCallResult(
        call_id="c1", name="mcp_rempilot_rempilot_remote_shell_open",
        arguments={}, result=raw, error=False,
    )
    state = _State([tr])
    ledger, text, _full = asyncio.run(_build_audit_evidence(state, budget=100000))
    assert "MIDDLE_MARKER_19767MiB" in text, (
        "middle of a long MCP result missing from evidence — MCP tools are "
        "still being fragmented (head+tail) instead of full-text grounding"
    )
    assert "完整" in ledger


def test_non_mcp_unknown_tool_stays_fragment():
    # 长结果（>800）才会被片段化——短结果本就走「完整(非grounding)」。
    raw = "HEAD\n" + "MID_MARKER_X\n" + ("m" * 2000) + "\nTAIL"
    tr = ToolCallResult(
        call_id="c2", name="some_unknown_tool", arguments={}, result=raw, error=False,
    )
    state = _State([tr])
    ledger, text, _full = asyncio.run(_build_audit_evidence(state, budget=100000))
    # 2026-09-20 审计完整性：非 grounding 片段标注从「片段」升级为「截断」，
    # 以便既有硬性约束（截断证据不得判 reject/编造）对非 grounding 证据同样生效。
    assert "截断(非grounding" in ledger
