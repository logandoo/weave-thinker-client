"""Integration checks for the real vibeweaver host wiring.

These tests execute the installed hook registry and the real ContextCompressor
delivery path; they do not stop at mocked service functions.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest


def _project(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir(parents=True)
    (tmp_path / "tests" / "acceptance.md").write_text(
        "> cap=5  stall=3×\n1. fixture\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "verification_log.md").write_text(
        "## current task\n- class: CODE — fixture\n- iter 1 PASS: fixture\n",
        encoding="utf-8",
    )
    return tmp_path


def test_real_tool_before_hooks_block_destructive_terminal(tmp_path: Path) -> None:
    from app.services import hook_service
    from app.services import vibeweaver_gate_service as gate

    root = _project(tmp_path)
    hook_service.clear_hooks()
    gate._installed = False
    try:
        summary = gate.install_hooks()
        assert summary.get("gate_tool_before_hooks") == 1
        decision = asyncio.run(hook_service.run_tool_hooks(
            "before",
            hook_service.ToolHookContext(
                tool_name="terminal",
                args={"command": "git add -A"},
                conversation_id="runtime-hook",
                workspace_path=str(root),
                vibeweaver_active=True,
            ),
        ))
        assert decision.block and "destructive" in decision.block.lower()
    finally:
        hook_service.clear_hooks()
        gate._installed = False


def test_real_tool_after_hook_runs_canonical_gate(tmp_path: Path) -> None:
    from app.services import hook_service
    from app.services import vibeweaver_gate_service as gate

    root = _project(tmp_path)
    hook_service.clear_hooks()
    gate._installed = False
    try:
        gate.install_hooks()
        decision = asyncio.run(hook_service.run_tool_hooks(
            "after",
            hook_service.ToolHookContext(
                tool_name="workspace_write",
                args={"file_path": "src/app.py"},
                conversation_id="runtime-gate",
                workspace_path=str(root),
                vibeweaver_active=True,
                result="write ok",
            ),
        ))
        assert decision.block and "GATE-BLOCKED" in decision.block
    finally:
        hook_service.clear_hooks()
        gate._installed = False


def test_real_stop_hook_blocks_red_gate(tmp_path: Path) -> None:
    from app.services import hook_service
    from app.services import vibeweaver_gate_service as gate

    root = _project(tmp_path)
    hook_service.clear_hooks()
    gate._installed = False
    try:
        gate.install_hooks()
        decision = asyncio.run(hook_service.run_stop_hooks(hook_service.StopContext(
            conversation_id="runtime-stop",
            workspace_path=str(root),
            assistant_content="Implemented the change.",
            vibeweaver_active=True,
            had_tool_calls=True,
        )))
        assert not decision.allow and "门禁拦截" in decision.feedback
    finally:
        hook_service.clear_hooks()
        gate._installed = False


def test_real_context_compressor_delivers_working_note_and_pending(tmp_path: Path) -> None:
    from app.services.context_compressor import ContextCompressor
    from app.services.vibeweaver_delivery_service import (
        consume_pending_resume,
        mark_pending_resume,
    )

    root = _project(tmp_path)
    (root / "tests" / "working_note.md").write_text(
        "# working note\n- confirmed: fixture\n- next: keep evidence\n",
        encoding="utf-8",
    )
    compressor = ContextCompressor(quiet=True)
    compressor.protect_first_n = 1
    compressor.protect_last_tokens = 1
    compressor.delivery_root = str(root)
    compressor.delivery_conv_id = "conv-compact"
    compressor.delivery_active = True
    captured: list[str] = []

    async def fake_summary(_turns, _focus_topic=None, delivery_context=""):
        captured.append(delivery_context or "")
        return "compacted summary"

    compressor._generate_summary_async = fake_summary  # type: ignore[method-assign]
    messages = [{"role": "user", "content": f"message {i} " + ("x" * 100)} for i in range(12)]
    out = asyncio.run(compressor.compress_async(messages))

    assert captured and "working note" in captured[0]
    assert any(m.get("_ephemeral") == "vibeweaver_compact_resume" for m in out)
    pending = consume_pending_resume(root, "conv-compact")
    assert pending and "working note" in pending
    assert consume_pending_resume(root, "conv-compact") is None


def test_pending_resume_is_one_shot_across_process_boundary(tmp_path: Path) -> None:
    from app.services.vibeweaver_delivery_service import (
        consume_pending_resume,
        mark_pending_resume,
    )

    root = _project(tmp_path)
    mark_pending_resume(root, "conv-cross")
    assert consume_pending_resume(root, "conv-cross") is not None
    assert consume_pending_resume(root, "conv-cross") is None


def _stop_context(root: Path, conv: str, text: str, user_text: str = "") -> object:
    from app.services.hook_service import StopContext

    return StopContext(
        conversation_id=conv,
        workspace_path=str(root),
        assistant_content=text,
        vibeweaver_active=True,
        had_tool_calls=True,
        last_user_text=user_text,
    )


def test_silent_stop_ignores_prose_and_report_writes(tmp_path: Path) -> None:
    from app.services import vibeweaver_stop_guard as guard

    root = _project(tmp_path)
    guard.reset_state()
    guard.touch_session("conv-prose")
    guard.note_task_write("conv-prose", "docs/report.md")
    guard.note_task_write("conv-prose", "reports/findings.json")
    decision = asyncio.run(guard._stop_guard_hook(_stop_context(root, "conv-prose", "Report is ready.")))
    assert decision is None


def test_silent_stop_ignores_awaiting_user_and_readonly_scope(tmp_path: Path) -> None:
    from app.services import vibeweaver_stop_guard as guard

    root = _project(tmp_path)
    guard.reset_state()
    guard.touch_session("conv-await")
    guard.note_task_write("conv-await", "src/app.py")
    assert asyncio.run(guard._stop_guard_hook(
        _stop_context(root, "conv-await", "Please confirm before I proceed.")
    )) is None
    assert asyncio.run(guard._stop_guard_hook(
        _stop_context(root, "conv-await", "Implementation changed.", "仅评估，不修改")
    )) is None


def test_audit_reads_completion_artifact_from_current_log_block(tmp_path: Path) -> None:
    from app.services import vibeweaver_audit_service as audit

    root = _project(tmp_path)
    (root / "tests" / "assert_artifacts.py").write_text("print(1)\n", encoding="utf-8")
    (root / "tests" / "verification_log.md").write_text(
        "## current task\n- class: CODE — fixture\n- iter 1 PASS: fixture\n"
        "- final-run: --final — pass=5/fail=0\n"
        "- assert-run: assert_artifacts.py: pass=5/fail=0\n"
        "[Verification Gate] Verifier: direct-read | Lane: S | Class: CODE | "
        "Tests executed with artifacts: yes | assert_artifacts.py: pass=5/fail=0 | "
        "HARD-GATE-1: NO-TEST-NO-DONE=pass | HARD-GATE-2: SCRIPT-ONLY=na\n"
        "| # | Problem | Research Sources (exa MCP / Context7) | Chosen Approach & Why "
        "| Files Changed | What Changed | Verification Evidence (Screenshot / Log) | Commit |\n",
        encoding="utf-8",
    )
    verdict = audit.audit_final_output("自然语言结果，不含仪式表格。", root)
    assert verdict["verdict"] == "ok", verdict


def test_audit_selects_task_block_not_trailing_notes(tmp_path: Path) -> None:
    from app.services import vibeweaver_audit_service as audit

    root = _project(tmp_path)
    (root / "tests" / "assert_artifacts.py").write_text("print(1)\n", encoding="utf-8")
    (root / "tests" / "verification_log.md").write_text(
        "## Task\n- class: CODE — fixture\n- iter 1 PASS: fixture\n"
        "- final-run: --final — pass=5/fail=0\n"
        "- assert-run: assert_artifacts.py: pass=5/fail=0\n"
        "[Verification Gate] Verifier: direct-read | Lane: S | Class: CODE | "
        "Tests executed with artifacts: yes | assert_artifacts.py: pass=5/fail=0 | "
        "HARD-GATE-1: NO-TEST-NO-DONE=pass | HARD-GATE-2: SCRIPT-ONLY=na\n"
        "| # | Problem | Research Sources (exa MCP / Context7) | Chosen Approach & Why "
        "| Files Changed | What Changed | Verification Evidence (Screenshot / Log) | Commit |\n"
        "\n## Notes\n- trailing note must not hide the task block\n",
        encoding="utf-8",
    )
    verdict = audit.audit_final_output("Done.", root)
    assert verdict["verdict"] == "ok", verdict


def test_stop_guard_honors_gate_marker_in_log(tmp_path: Path) -> None:
    from app.services import vibeweaver_stop_guard as guard

    root = _project(tmp_path)
    (root / "tests" / "verification_log.md").write_text(
        "## current task\n- class: CODE — fixture\n- iter 1 PASS: fixture\n"
        "[Verification Gate] Verifier: direct-read | HARD-GATE-1: NO-TEST-NO-DONE=pass\n"
        "\n## Notes\n- trailing note must not cause a false silent-stop\n",
        encoding="utf-8",
    )
    guard.reset_state()
    guard.touch_session("conv-log-gate")
    guard.note_task_write("conv-log-gate", "src/app.py")
    decision = asyncio.run(guard._stop_guard_hook(_stop_context(root, "conv-log-gate", "Implemented.")))
    assert decision is None


def test_silent_stop_rejects_zero_or_unbound_final_run(tmp_path: Path) -> None:
    from app.services import vibeweaver_stop_guard as guard

    root = _project(tmp_path)
    (root / "tests" / "verification_log.md").write_text(
        "## current task\n- class: CODE — fixture\n- iter 1 PASS: fixture\n"
        "- final-run: --final — pass=0/fail=0\n",
        encoding="utf-8",
    )
    guard.reset_state()
    guard.touch_session("conv-zero")
    guard.note_task_write("conv-zero", "src/app.py")
    decision = asyncio.run(guard._stop_guard_hook(_stop_context(root, "conv-zero", "Changed the implementation.")))
    assert decision is not None and not decision.allow

    (root / "tests" / "verification_log.md").write_text(
        "## current task\n- class: CODE — fixture\n- iter 1 PASS: fixture\n"
        "- final-run: --final — pass=5/fail=0\n- assert-run: assert_artifacts.py: pass=5/fail=0\n",
        encoding="utf-8",
    )
    decision = asyncio.run(guard._stop_guard_hook(_stop_context(root, "conv-zero", "Changed the implementation.")))
    assert decision is None
