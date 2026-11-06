"""Host gate trust features that upstream vibeweaver-gate.js enforces before tools.

These are host adaptations (Python hooks), not OpenCode plugin tests.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


def _project(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir(parents=True)
    (tmp_path / "tests" / "verification_log.md").write_text(
        "## current task\n- class: CODE — test fixture\n- iter 1 PASS: fixture\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "acceptance.md").write_text(
        "> cap=5  stall=3×\n1. fixture\n",
        encoding="utf-8",
    )
    return tmp_path


def test_risk_path_requires_explicit_approval(tmp_path: Path) -> None:
    from app.services import vibeweaver_gate_service as gate

    root = _project(tmp_path)
    block = gate.risk_path_block(root, "backend/auth/login.py")
    assert block and "risk-tier" in block.lower()
    (root / "tests" / "decisions.md").write_text(
        "- risk-approved: backend/auth/login.py — user approved auth change\n",
        encoding="utf-8",
    )
    assert gate.risk_path_block(root, "backend/auth/login.py") is None
    assert gate.risk_path_block(root, "backend/public/readme.py") is None


def test_destructive_commands_are_blocked_with_approval_path(tmp_path: Path) -> None:
    from app.services import vibeweaver_gate_service as gate

    root = _project(tmp_path)
    assert gate.destructive_block(root, "git add -A")
    assert gate.destructive_block(root, "git reset --hard")
    assert gate.destructive_block(root, "rm -rf src")
    assert gate.destructive_block(root, "rm -rf node_modules") is None
    (root / "tests" / "decisions.md").write_text(
        "- destructive-approved: git add -A — user approved staging\n",
        encoding="utf-8",
    )
    assert gate.destructive_block(root, "git add -A") is None


def test_risk_command_blocks_rm_of_risk_path(tmp_path: Path) -> None:
    from app.services import vibeweaver_gate_service as gate

    root = _project(tmp_path)
    assert gate.risk_command_block(root, "rm -f backend/security/policy.py")
    assert gate.risk_command_block(root, "rm -f backend/public/readme.py") is None


def test_risk_approval_uses_exact_path_not_basename(tmp_path: Path) -> None:
    from app.services import vibeweaver_gate_service as gate

    root = _project(tmp_path)
    (root / "tests" / "decisions.md").write_text(
        "- risk-approved: opencode/packages/server/src/auth.ts — unrelated file\n",
        encoding="utf-8",
    )
    assert gate.risk_command_block(root, "rm -rf backend/auth")
    assert gate.risk_command_block(root, "git rm -r backend/auth")
    assert gate.destructive_block(root, "rm -rf /tmp/../etc")
    assert gate.destructive_block(root, "rm -Rf src")
    assert gate.destructive_block(root, "rm --recursive --force src")
    assert gate.destructive_block(root, "git rm -r src")
    assert gate.destructive_block(root, "git clean --force -d")
    assert gate.destructive_block(root, "git stash -u")
    assert gate.destructive_block(root, "rg -n 'git add -A' docs/") is None


def test_doc_class_cannot_write_code(tmp_path: Path) -> None:
    from app.services import vibeweaver_gate_service as gate

    root = _project(tmp_path)
    (root / "tests" / "verification_log.md").write_text(
        "## current task\n- class: DOC — prose only\n- iter 1 PASS: fixture\n",
        encoding="utf-8",
    )
    assert gate.doc_class_block(root, "src/app.py")
    assert gate.doc_class_block(root, "src/App.vue")
    assert gate.doc_class_block(root, "docs/readme.md") is None


def test_doc_class_guard_ignores_trailing_notes_section(tmp_path: Path) -> None:
    from app.services import vibeweaver_gate_service as gate

    root = _project(tmp_path)
    (root / "tests" / "verification_log.md").write_text(
        "## Doc task\n- class: DOC — prose\n- iter 1 PASS: prose\n"
        "\n## Notes\n- [Verification Gate] quoted in notes\n",
        encoding="utf-8",
    )
    assert gate.doc_class_block(root, "src/App.vue")


def test_protected_evidence_lock_direct_write_blocks(tmp_path: Path) -> None:
    from app.services import vibeweaver_gate_service as gate

    root = _project(tmp_path)
    block = asyncio.run(gate.check_protected_evidence_async(root, "tests/.evidence.lock"))
    assert block and block.get("block") and "evidence lock" in block["block"].lower()


def test_protected_evidence_missing_lock_after_evidence_blocks(tmp_path: Path) -> None:
    from app.services import vibeweaver_gate_service as gate

    root = _project(tmp_path)
    block = asyncio.run(gate.check_protected_evidence_async(root, "tests/acceptance.md"))
    assert block and block.get("block") and "tamper" in block["block"].lower()


def test_acceptance_is_refinable_before_evidence_then_frozen(tmp_path: Path) -> None:
    from app.services import vibeweaver_gate_service as gate

    root = _project(tmp_path)
    (root / "tests" / "verification_log.md").write_text(
        "## current task\n- class: CODE — fixture\n", encoding="utf-8"
    )
    assert asyncio.run(gate.check_protected_evidence_async(root, "tests/acceptance.md")) is None
    (root / "tests" / "acceptance.md").write_text("> cap=5  stall=3×\n1. changed\n", encoding="utf-8")
    assert asyncio.run(gate.check_protected_evidence_async(root, "tests/acceptance.md")) is None

    (root / "tests" / "verification_log.md").write_text(
        "## current task\n- class: CODE — fixture\n- iter 1 PASS: fixture\n", encoding="utf-8"
    )
    (root / "tests" / "acceptance.md").write_text("> cap=5  stall=3×\n1. frozen\n", encoding="utf-8")
    result = asyncio.run(gate.check_protected_evidence_async(root, "tests/acceptance.md"))
    assert result and result.get("block")


def test_gate_write_checks_protected_evidence_before_exemption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import vibeweaver_gate_service as gate

    root = _project(tmp_path)
    monkeypatch.setattr(gate, "get_config", lambda: SimpleNamespace(vibeweaver_gate_enabled=True))
    monkeypatch.setattr(gate, "_evidence_lock_script", lambda _root: Path("/tmp/evidence_lock.py"))
    result = asyncio.run(gate.gate_check_write_async(root, "tests/acceptance.md"))
    assert result and result.get("block")
    # Ordinary evidence writes remain exempt.
    assert asyncio.run(gate.gate_check_write_async(root, "tests/new-evidence.md")) is None


def test_ensure_assert_script_executes_bundled_pin_not_workspace_copy(tmp_path: Path) -> None:
    from app.services import vibeweaver_gate_service as gate

    root = _project(tmp_path)
    target = root / "tests" / "assert_artifacts.py"
    target.write_bytes(gate._canonical_assert_script().read_bytes())
    selected = gate._ensure_assert_script(root)
    assert selected == gate._canonical_assert_script()
    assert selected != target


class _SplitProcess:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout.encode(), self._stderr.encode()

    def kill(self) -> None:
        return None

    async def wait(self) -> None:
        return None


def test_green_pinned_check_requires_valid_stdout_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import vibeweaver_gate_service as gate

    async def fake_exec(*args: str, **kwargs: object) -> _SplitProcess:
        return _SplitProcess("", "{diagnostic: not-json}", 0)

    monkeypatch.setattr(gate.asyncio, "create_subprocess_exec", fake_exec)
    ok, _attempts = asyncio.run(gate.run_assert(tmp_path, "python3"))
    assert not ok


def test_stdout_json_is_not_masked_by_stderr_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import vibeweaver_gate_service as gate

    async def fake_exec(*args: str, **kwargs: object) -> _SplitProcess:
        return _SplitProcess('{"ok": true, "passes": 3, "fails": []}', '{"note": "stderr"}', 0)

    monkeypatch.setattr(gate.asyncio, "create_subprocess_exec", fake_exec)
    ok, attempts = asyncio.run(gate.run_assert(tmp_path, "python3"))
    assert ok and attempts[0]["warnings"] == []


def test_unknown_json_severity_blocks() -> None:
    from app.services import vibeweaver_gate_service as gate

    blocking, warnings = gate._classify([("future-severity", "error"), ("advisory", "warn")])
    assert blocking == ["future-severity"]
    assert warnings == ["advisory"]


def test_green_run_surfaces_warnings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import vibeweaver_gate_service as gate

    async def fake_exec(*args: str, **kwargs: object) -> _SplitProcess:
        return _SplitProcess('{"ok": true, "fails": [{"severity": "warn", "detail": "stale seal"}]}', "", 0)

    monkeypatch.setattr(gate.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(gate, "_ensure_assert_script", lambda _root: Path("/tmp/checker.py"))
    monkeypatch.setattr(gate, "_canonical_assert_script", lambda: None)
    monkeypatch.setattr(gate, "_legacy_resource_resolver_missing", lambda: False)
    result = asyncio.run(gate.check_gate_async(tmp_path))
    assert result and result["warnings"] == ["stale seal"] and not result["blocking"]


def test_stale_recovery_file_does_not_wedge_delivery(tmp_path: Path) -> None:
    from app.services import vibeweaver_delivery_service as delivery
    import time

    root = _project(tmp_path)
    recovery = root / ".vibeweaver" / "delivery-state.recovery"
    recovery.parent.mkdir(parents=True)
    recovery.write_text("crashed-owner", encoding="utf-8")
    started = time.monotonic()
    delivery.mark_pending_resume(root, "after-crash")
    assert delivery.consume_pending_resume(root, "after-crash")
    assert time.monotonic() - started < 2.0


def test_before_hook_blocks_direct_evidence_lock_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import hook_service
    from app.services import vibeweaver_gate_service as gate

    root = _project(tmp_path)
    monkeypatch.setattr(gate, "get_config", lambda: SimpleNamespace(vibeweaver_gate_enabled=True))
    decision = asyncio.run(gate._tool_before_hook(hook_service.ToolHookContext(
        tool_name="workspace_write",
        args={"file_path": "tests/.evidence.lock"},
        conversation_id="conv-lock",
        workspace_path=str(root),
        vibeweaver_active=True,
    )))
    assert decision and decision.block and "evidence lock" in decision.block.lower()


def test_before_hook_blocks_risk_write_and_destructive_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import hook_service
    from app.services import vibeweaver_gate_service as gate

    root = _project(tmp_path)
    monkeypatch.setattr(gate, "get_config", lambda: SimpleNamespace(vibeweaver_gate_enabled=True))
    write_ctx = hook_service.ToolHookContext(
        tool_name="workspace_write",
        args={"file_path": "backend/auth/login.py"},
        conversation_id="conv-risk",
        workspace_path=str(root),
        vibeweaver_active=True,
    )
    decision = asyncio.run(gate._tool_before_hook(write_ctx))
    assert decision and decision.block

    term_ctx = hook_service.ToolHookContext(
        tool_name="terminal",
        args={"command": "git add -A"},
        conversation_id="conv-risk",
        workspace_path=str(root),
        vibeweaver_active=True,
    )
    decision = asyncio.run(gate._tool_before_hook(term_ctx))
    assert decision and decision.block

    process_ctx = hook_service.ToolHookContext(
        tool_name="process",
        args={"command": "rm -rf src"},
        conversation_id="conv-risk",
        workspace_path=str(root),
        vibeweaver_active=True,
    )
    decision = asyncio.run(gate._tool_before_hook(process_ctx))
    assert decision and decision.block
