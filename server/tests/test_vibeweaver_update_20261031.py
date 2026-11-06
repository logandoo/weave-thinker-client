"""vibeweaver 2026-10-31 上游刷新与宿主适配回归。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
SKILL_ROOT = BACKEND_ROOT / "skills" / "vibeweaver"
# 2026-11-06 W38：规范包 checker 随全局 canonical（dev skill release 3af8820d）前移
# ——旧 pin 46af9c20 对应 upstream-chatbot vendored 的旧版 skill 副本；两侧 pin 冲突
# （dev 门禁要求 repo 副本 == canonical，产品测试要求 repo 副本 == 随包副本），
# 裁决 = 随包副本/仓内副本/pin/本常量四处同步到 canonical（ADR D-131）。
CHECKER_SHA256 = "3af8820dade3b5988d1742b6a33cdf0e7ddb24ee43b5bebc144e45142f5c0a5b"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_canonical_checker_pack_and_pin() -> None:
    bundled = SKILL_ROOT / "scripts" / "assert_artifacts.py"
    pin = SKILL_ROOT / "scripts" / "assert_artifacts.sha256"
    assert pin.is_file(), "bundled checker pin is missing"
    assert pin.read_text(encoding="utf-8").split()[0] == CHECKER_SHA256
    assert _sha256(bundled) == CHECKER_SHA256
    assert _sha256(REPO_ROOT / "tests" / "assert_artifacts.py") == CHECKER_SHA256


def test_contract_companions_cov0_and_size_budget() -> None:
    assert (SKILL_ROOT / "AUDIT_PLUGIN.md").is_file()
    assert (SKILL_ROOT / "bootstrap.md").is_file()
    assert (SKILL_ROOT / "NOTICE").is_file()
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert len(skill.encode("utf-8")) < 49_000
    for marker in ("交付契约", "不得作为用户可见", "最终回答", "[Verification Gate]"):
        assert marker in skill
    for name in (
        "APPENDIX.md",
        "AUDIT_PLUGIN.md",
        "COMPLETION_GATE.md",
        "ENGINEERING_STD.md",
        "MEMORY_RULES.md",
        "MEMORY_TEMPLATES.md",
        "REFERENCE.md",
        "TESTING_PROTOCOLS.md",
        "VERIFICATION_UPGRADES.md",
        "WORKFLOWS_EXTENDED.md",
    ):
        assert len((SKILL_ROOT / name).read_bytes()) <= 45_000, name


def test_runtime_script_pack_and_root_helper_parity() -> None:
    required = (
        "assert_artifacts.py",
        "assert_artifacts.sha256",
        "mm_probe.py",
        "scan_secrets.py",
        "evidence_lock.py",
        "evidence_seal.py",
        "ui_fidelity.py",
        "backlog_check.py",
        "record_evidence.sh",
        "make_review_package.sh",
        "repair_checker.sh",
        "state_sanitize.py",
    )
    for name in required:
        assert (SKILL_ROOT / "scripts" / name).is_file(), name
    for name in ("evidence_lock.py", "evidence_seal.py", "scan_secrets.py"):
        assert (REPO_ROOT / "scripts" / name).read_bytes() == (SKILL_ROOT / "scripts" / name).read_bytes()


def test_stale_workspace_checker_uses_canonical_without_overwrite(tmp_path: Path) -> None:
    from app.services import vibeweaver_gate_service as gate

    target = tmp_path / "tests" / "assert_artifacts.py"
    target.parent.mkdir(parents=True)
    target.write_text("# custom stale checker\n", encoding="utf-8")
    selected = gate._ensure_assert_script(tmp_path)
    assert selected == SKILL_ROOT / "scripts" / "assert_artifacts.py"
    assert target.read_text(encoding="utf-8") == "# custom stale checker\n"


class _FakeProcess:
    def __init__(self, output: str, returncode: int = 1) -> None:
        self._output = output
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._output.encode(), b""

    def kill(self) -> None:
        return None

    async def wait(self) -> None:
        return None


def test_run_assert_uses_json_severity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import vibeweaver_gate_service as gate

    payload = json.dumps({"ok": False, "fails": [
        {"severity": "warn", "detail": "advisory-only"},
        {"severity": "block", "detail": "hard-failure"},
    ]})
    seen: list[tuple[str, ...]] = []

    async def fake_exec(*args: str, **kwargs: object) -> _FakeProcess:
        seen.append(args)
        return _FakeProcess(payload)

    monkeypatch.setattr(gate.asyncio, "create_subprocess_exec", fake_exec)
    ok, attempts = asyncio.run(gate.run_assert(tmp_path, "python3"))
    assert not ok
    blocking, warnings = gate._classify(gate._failure_messages(attempts))
    assert blocking == ["hard-failure"]
    assert warnings == ["advisory-only"]
    assert seen and all("--json" in call for call in seen)
    assert seen and all("--root" in call and str(tmp_path) in call for call in seen)


def test_run_assert_malformed_output_is_blocking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import vibeweaver_gate_service as gate

    async def fake_exec(*args: str, **kwargs: object) -> _FakeProcess:
        return _FakeProcess("not-json")

    monkeypatch.setattr(gate.asyncio, "create_subprocess_exec", fake_exec)
    ok, attempts = asyncio.run(gate.run_assert(tmp_path, "python3"))
    assert not ok
    blocking, _ = gate._classify(gate._failure_messages(attempts))
    assert blocking and all("not-json" in item for item in blocking)


def test_cue_glob_has_total_star_budget() -> None:
    from app.services import vibeweaver_delivery_service as delivery

    assert delivery.safe_glob("a*b*c")
    assert delivery.safe_glob("a**b")
    assert not delivery.safe_glob("a*b**c")  # 3 个总星号 > 上游 cap=2
    assert not delivery.safe_glob("a*b*c*d*e")
    assert not delivery.safe_glob("x" * 65)


def test_deathmatch_compressor_attaches_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.context_compressor import ContextCompressor
    from app.services.deathmatch_plan_core import DeathmatchPlanCoreMixin

    captured: list[ContextCompressor] = []

    class FakeCompressor:
        def __init__(self, quiet: bool) -> None:
            self.quiet = quiet
            captured.append(self)

        async def compress_async(self, messages: list[dict], focus_topic: str | None = None) -> list[dict]:
            return messages

    monkeypatch.setattr("app.services.context_compressor.ContextCompressor", FakeCompressor)
    owner = DeathmatchPlanCoreMixin()
    owner._workspace_path = "/tmp/workspace"
    owner._conv = SimpleNamespace(id="conv-1")
    owner.vibeweaver_active = False
    asyncio.run(owner.compress_messages([]))
    assert captured[0].delivery_root == "/tmp/workspace"
    assert captured[0].delivery_conv_id == "conv-1"
    assert captured[0].delivery_active is False


def test_state_save_redacts_credentials_and_preserves_unknown_fields(tmp_path: Path) -> None:
    from app.services import vibeweaver_state

    secret = "sk-" + "a" * 20
    vibeweaver_state.save_state(tmp_path, {"unknown": {"command": f"api_key={secret}"}, "keep": 7})
    raw = (tmp_path / ".vibeweaver" / "state.json").read_text(encoding="utf-8")
    assert secret not in raw
    assert "REDACTED" in raw
    assert json.loads(raw)["keep"] == 7


def test_state_redacts_unterminated_private_key(tmp_path: Path) -> None:
    from app.services import vibeweaver_state

    marker = "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nnot-a-real-key"
    vibeweaver_state.save_state(tmp_path, {"command": marker})
    raw = (tmp_path / ".vibeweaver" / "state.json").read_text(encoding="utf-8")
    assert "BEGIN OPENSSH PRIVATE KEY" not in raw
    assert "REDACTED:private-key-block" in raw


def test_tool_observation_command_is_bounded() -> None:
    from app.services import vibeweaver_audit_service as audit

    session = "vibeweaver-update-long-command"
    command = "x" * 20_000
    audit.observe_tool_call(session, "terminal", command)
    stored = audit._obs(session).tool_observations[-1]["command"]
    assert len(stored) < 9_000
    assert stored.startswith("x" * 100)
    assert stored.endswith("x" * 100)


def test_stop_guard_detects_silent_task_write(tmp_path: Path) -> None:
    from app.services import vibeweaver_stop_guard as guard
    from app.services.hook_service import StopContext

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "acceptance.md").write_text("> cap=5  stall=3×\n", encoding="utf-8")
    (tmp_path / "tests" / "verification_log.md").write_text("## current\n- iter 1 FAIL: c1\n", encoding="utf-8")
    guard.reset_state()
    guard.touch_session("conv-silent")
    guard.note_task_write("conv-silent", "src/changed.py")
    decision = asyncio.run(guard._stop_guard_hook(StopContext(
        conversation_id="conv-silent",
        workspace_path=str(tmp_path),
        assistant_content="Changed the implementation.",
        had_tool_calls=True,
    )))
    assert decision is not None and not decision.allow
    assert "silent" in decision.feedback.lower() or "stopped" in decision.feedback.lower()


def test_stop_guard_ignores_evidence_only_write(tmp_path: Path) -> None:
    from app.services import vibeweaver_stop_guard as guard
    from app.services.hook_service import StopContext

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "acceptance.md").write_text("> cap=5  stall=3×\n", encoding="utf-8")
    (tmp_path / "tests" / "verification_log.md").write_text("## current\n- iter 1 FAIL: c1\n", encoding="utf-8")
    guard.reset_state()
    guard.touch_session("conv-evidence")
    guard.note_task_write("conv-evidence", "tests/evidence.log")
    decision = asyncio.run(guard._stop_guard_hook(StopContext(
        conversation_id="conv-evidence",
        workspace_path=str(tmp_path),
        assistant_content="Updated the evidence.",
        had_tool_calls=True,
    )))
    assert decision is None


def test_production_deathmatch_compression_resolves_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.deathmatch_plan_core import DeathmatchPlanCoreMixin

    async def fake_workspace(_db, _user_id):
        return SimpleNamespace(root_path="/tmp/production-workspace")

    async def fake_resolve(messages, _db):
        return messages

    monkeypatch.setattr("app.services.workspace_service.ensure_user_workspace", fake_workspace)
    monkeypatch.setattr("app.services.message_payload_service.resolve_message_fields", fake_resolve)

    class FakeResult:
        def scalars(self):
            return SimpleNamespace(all=lambda: [])

    class FakeDB:
        async def execute(self, _query):
            return FakeResult()

        async def flush(self):
            return None

    owner = DeathmatchPlanCoreMixin()
    owner._conv = SimpleNamespace(
        id="conv-production", user_id="user-1", deathmatch_goal="goal",
        deathmatch_context_summary=None, deathmatch_compressed_context=None,
    )
    monkeypatch.setattr(owner, "generate_handoff_document", lambda: "")
    asyncio.run(owner.compress_conversation_context(FakeDB()))
    assert owner._workspace_path == "/tmp/production-workspace"


def test_missing_canonical_resource_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import vibeweaver_gate_service as gate

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "assert_artifacts.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    monkeypatch.setattr(gate, "_canonical_assert_script", lambda: None)
    result = asyncio.run(gate.check_gate_async(tmp_path))
    assert result and result["blocking"]
    assert "canonical" in result["blocking"][0].lower()


def test_missing_pin_production_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import vibeweaver_gate_service as gate

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "assert_artifacts.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    monkeypatch.setattr(gate, "_canonical_assert_script", lambda: SKILL_ROOT / "scripts" / "assert_artifacts.py")
    monkeypatch.setattr(gate, "_canonical_checker_pin", lambda _src: "")
    result = asyncio.run(gate.check_gate_async(tmp_path))
    assert result and result["blocking"]
    assert "pin" in result["blocking"][0].lower()


def test_bundled_pin_mismatch_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import vibeweaver_gate_service as gate

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "assert_artifacts.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    monkeypatch.setattr(gate, "_canonical_assert_script", lambda: SKILL_ROOT / "scripts" / "assert_artifacts.py")
    monkeypatch.setattr(gate, "_canonical_checker_pin", lambda _src: "0" * 64)
    result = asyncio.run(gate.check_gate_async(tmp_path))
    assert result and result["blocking"]
    assert "pin" in result["blocking"][0].lower()


def test_deathmatch_mode_off_disables_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.deathmatch_plan_core import DeathmatchPlanCoreMixin

    captured = []

    class FakeCompressor:
        def __init__(self, quiet: bool) -> None:
            captured.append(self)

        async def compress_async(self, messages: list[dict], focus_topic: str | None = None) -> list[dict]:
            return messages

    monkeypatch.setattr("app.services.context_compressor.ContextCompressor", FakeCompressor)
    monkeypatch.setattr("app.core.config.get_config", lambda: SimpleNamespace(vibeweaver_mode="off"))
    owner = DeathmatchPlanCoreMixin()
    owner._workspace_path = "/tmp/workspace"
    owner._conv = SimpleNamespace(id="conv-off")
    asyncio.run(owner.compress_messages([]))
    assert captured[0].delivery_active is False


def test_deathmatch_missing_active_attribute_uses_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.deathmatch_plan_core import DeathmatchPlanCoreMixin

    captured = []

    class FakeCompressor:
        def __init__(self, quiet: bool) -> None:
            captured.append(self)

        async def compress_async(self, messages: list[dict], focus_topic: str | None = None) -> list[dict]:
            return messages

    monkeypatch.setattr("app.services.context_compressor.ContextCompressor", FakeCompressor)
    monkeypatch.setattr("app.core.config.get_config", lambda: SimpleNamespace(vibeweaver_mode="auto"))
    owner = DeathmatchPlanCoreMixin()
    owner._workspace_path = "/tmp/workspace"
    owner._conv = SimpleNamespace(id="conv-no-active-attr")
    asyncio.run(owner.compress_messages([]))
    assert captured[0].delivery_active is False  # auto + no per-turn decision is inactive

    captured.clear()
    monkeypatch.setattr("app.core.config.get_config", lambda: SimpleNamespace(vibeweaver_mode="always"))
    owner = DeathmatchPlanCoreMixin()
    owner._workspace_path = "/tmp/workspace"
    owner._conv = SimpleNamespace(id="conv-always")
    asyncio.run(owner.compress_messages([]))
    assert captured[0].delivery_active is True


def test_delivery_lock_recovers_stale_file(tmp_path: Path) -> None:
    from app.services import vibeweaver_delivery_service as delivery

    lock = tmp_path / ".vibeweaver" / "delivery-state.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("stale", encoding="utf-8")
    old = time.time() - 120
    os.utime(lock, (old, old))
    delivery.mark_pending_resume(tmp_path, "stale-recovery")
    state = json.loads((tmp_path / ".vibeweaver" / "delivery-state.json").read_text(encoding="utf-8"))
    assert state["pendingResume"]["stale-recovery"] > 0


def test_concurrent_stale_recovery_preserves_all_markers(tmp_path: Path) -> None:
    from concurrent.futures import ProcessPoolExecutor
    from app.services import vibeweaver_delivery_service as delivery

    lock = tmp_path / ".vibeweaver" / "delivery-state.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("stale-owner", encoding="utf-8")
    old = time.time() - 120
    os.utime(lock, (old, old))
    with ProcessPoolExecutor(max_workers=16) as pool:
        list(pool.map(_mark_worker, [str(tmp_path)] * 16, [f"stale-{i}" for i in range(16)]))
    state = json.loads((tmp_path / ".vibeweaver" / "delivery-state.json").read_text(encoding="utf-8"))
    assert set(state["pendingResume"]) == {f"stale-{i}" for i in range(16)}


def test_touch_session_clears_write_history() -> None:
    from app.services import vibeweaver_stop_guard as guard

    guard.reset_state()
    guard.touch_session("conv-rerun")
    guard.note_task_write("conv-rerun", "src/old.py")
    guard.touch_session("conv-rerun")
    assert guard._session_state("conv-rerun")["writes"] == []


def _consume_worker(root: str, conversation_id: str) -> bool:
    from app.services import vibeweaver_delivery_service as delivery
    return delivery.consume_pending_resume(Path(root), conversation_id) is not None


def _mark_worker(root: str, conversation_id: str) -> None:
    from app.services import vibeweaver_delivery_service as delivery
    delivery.mark_pending_resume(Path(root), conversation_id)


def test_delivery_lock_allows_only_one_consumer_and_preserves_marks(tmp_path: Path) -> None:
    from concurrent.futures import ProcessPoolExecutor
    from app.services import vibeweaver_delivery_service as delivery

    delivery.mark_pending_resume(tmp_path, "shared")
    with ProcessPoolExecutor(max_workers=2) as pool:
        consumed = sorted(pool.map(_consume_worker, [str(tmp_path)] * 2, ["shared"] * 2))
    assert consumed == [False, True]
    with ProcessPoolExecutor(max_workers=4) as pool:
        list(pool.map(_mark_worker, [str(tmp_path)] * 8, [f"conv-{i}" for i in range(8)]))
    state = json.loads((tmp_path / ".vibeweaver" / "delivery-state.json").read_text(encoding="utf-8"))
    assert set(state["pendingResume"]) == {f"conv-{i}" for i in range(8)}


def test_redaction_preserves_deep_state_and_list_lengths(tmp_path: Path) -> None:
    from app.services import vibeweaver_state

    secret = "sk-" + "b" * 20
    nested: dict = {"command": f"api_key={secret}"}
    root = nested
    for _ in range(12):
        root["child"] = {}
        root = root["child"]
    payload = {"nested": nested, "items": list(range(501))}
    vibeweaver_state.save_state(tmp_path, payload)
    raw = (tmp_path / ".vibeweaver" / "state.json").read_text(encoding="utf-8")
    assert secret not in raw
    assert len(json.loads(raw)["items"]) == 501
