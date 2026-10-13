"""vibeweaver audit + loop-guard（P2）— A4.8 test-first。

覆盖：noop-bash 检测（字面 echo/printf/true/:、截断打断、同 pid 去重、
≥4 连续）；文本退化检测（行组周期重复、裸递增序列、字符级周期）；
claim↔产物交叉校验（HARD-GATE token / artifacts 声明 / 媒体非空 / 表头）；
RED latch（会话作用域、换会话/TTL 自动释放并留痕 gate_audit.md）；
latch 封写（证据路径豁免）；干预预算（每会话每通道 2 次）；
message.delta → 观察 → 纠正指令注入 state.loop_guard_feedback。
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import hook_service as hs  # noqa: E402
from app.services import vibeweaver_audit_service as aw  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    hs.clear_hooks()
    aw._observations.clear()
    aw._active_runs.clear()
    aw._skill_activations.clear()
    aw._c17_warned.clear()
    aw._terminal_write_warned.clear()
    yield
    hs.clear_hooks()
    aw._observations.clear()
    aw._active_runs.clear()
    aw._skill_activations.clear()
    aw._c17_warned.clear()
    aw._terminal_write_warned.clear()


# ---------------- noop-bash ----------------

def test_noop_bash_literals():
    assert aw.is_noop_bash_command("echo done")
    assert aw.is_noop_bash_command("/bin/echo 'final'")
    assert aw.is_noop_bash_command("printf '%s' ok")
    assert aw.is_noop_bash_command("true")
    assert aw.is_noop_bash_command(":")
    assert aw.is_noop_bash_command("echo final;")  # 尾部单个 ;
    assert not aw.is_noop_bash_command("echo $HOME")           # 展开 = 真命令
    assert not aw.is_noop_bash_command("echo a && ls")          # 结构
    assert not aw.is_noop_bash_command("echo\nls")              # 换行链
    assert not aw.is_noop_bash_command("git status")


def test_noop_bash_finding_run_of_four():
    tools = [{"tool": "bash", "command": f"echo narration-{i}"} for i in range(4)]
    f = aw.noop_bash_finding(tools)
    assert f and f["kind"] == "noop-bash" and "4 consecutive" in f["pattern"]
    # 第 3 个是真命令 → 不触发
    tools2 = tools[:2] + [{"tool": "bash", "command": "ls -la"}] + tools[3:]
    assert aw.noop_bash_finding(tools2) is None
    # 截断命令打断连续
    tools3 = [{"tool": "bash", "command": "echo x", "truncated": True}] + tools[:3]
    assert aw.noop_bash_finding(tools3) is None
    # 非 bash 工具打断
    tools4 = [{"tool": "read", "command": "x"}] + tools[:3]
    assert aw.noop_bash_finding(tools4) is None


# ---------------- 文本退化 ----------------

def test_loop_finding_line_group_repeat():
    block = "hello world line\nsecond content line"
    text = "prologue\n" + (block + "\n") * 3
    f = aw.loop_finding(text)
    assert f and f["kind"] == "repeat" and "hello world line" in f["pattern"]


def test_loop_finding_single_line_needs_four():
    one = "2026-10-01 checkpoint written"
    text4 = ("tail head\n" + one + "\n") * 4
    f = aw.loop_finding(text4)
    assert f and f["kind"] == "repeat"


def test_loop_finding_stepping_tokens():
    text = "start\n" + " ".join(str(i) for i in range(1, 25))
    f = aw.loop_finding(text)
    # pattern = 尾部 12 个连续 token（判定作用域即尾部游程）
    assert f and f["kind"] == "sequence" and f["pattern"].endswith("24")
    letters = "start\n" + " ".join(aw_26(i) for i in range(1, 25))
    f2 = aw.loop_finding(letters)
    assert f2 and f2["kind"] == "sequence"


def aw_26(i: int) -> str:
    """bijective base-26（a..z, aa..）。"""
    s = ""
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(97 + r) + s
    return s


def test_loop_finding_clean_text():
    assert aw.loop_finding("This is a perfectly normal assistant answer with real words and no repetition patterns at all.") is None


# ---------------- claim 校验 + RED latch ----------------

def _mk_root(tmp_path):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "verification_log.md").write_text("- iter 1 PASS: c1\n", encoding="utf-8")
    return root


def _gate_content(extra=""):
    return (
        "[Verification Gate] Verifier: direct-read | Lane: S | Class: CODE | "
        "Tests executed with artifacts: yes | assert_artifacts.py: pass=5/fail=0 | "
        "HARD-GATE-1: NO-TEST-NO-DONE=pass | HARD-GATE-2: SCRIPT-ONLY=na (doc)\n"
        "| # | Problem | Research Sources (exa MCP / Context7) | Chosen Approach & Why "
        "| Files Changed | What Changed | Verification Evidence (Screenshot / Log) | Commit |\n"
        + extra
    )


def test_audit_final_output_ok_bad_skip(tmp_path):
    root = _mk_root(tmp_path)
    (root / "tests" / "verification_log.md").write_text("- iter 1 PASS: c1\n", encoding="utf-8")
    (root / "tests" / "assert_artifacts.py").write_text("print(1)\n", encoding="utf-8")
    ok = aw.audit_final_output(_gate_content(), root)
    assert ok["verdict"] == "ok", ok

    skip = aw.audit_final_output("普通回答，无门禁行", root)
    assert skip["verdict"] == "skip"

    bad = aw.audit_final_output("[Verification Gate] Tests executed with artifacts: yes", root)
    assert bad["verdict"] == "bad"
    text = " ".join(bad["reasons"])
    assert "HARD-GATE" in text


def test_red_latch_blocks_writes_and_releases(tmp_path, monkeypatch):
    root = _mk_root(tmp_path)
    monkeypatch.setattr(aw, "get_config",
                        lambda: type("C", (), {"vibeweaver_red_ttl_hours": 24, "vibeweaver_audit_enabled": True})())
    aw.latch_red(root, "s1", ["reason-a"])
    red, reasons = aw.red_state(root, "s1")
    assert red and "reason-a" in reasons

    # 封写（非证据路径）
    async def scenario():
        d = await aw._audit_tool_before(hs.ToolHookContext(
            tool_name="workspace_write",
            args={"file_path": str(root / "src" / "a.js")},
            conversation_id="s1",
            workspace_path=str(root),
        ))
        assert d and d.block and "RED-LATCHED" in d.block
        # 证据路径豁免
        d2 = await aw._audit_tool_before(hs.ToolHookContext(
            tool_name="workspace_write",
            args={"file_path": str(root / "tests" / "verification_log.md")},
            conversation_id="s1",
            workspace_path=str(root),
        ))
        assert d2 is None
    asyncio.run(scenario())

    # 换会话自动释放 + 留痕
    red2, _ = aw.red_state(root, "other-session")
    assert not red2
    journal = (root / "tests" / "gate_audit.md").read_text(encoding="utf-8")
    assert "Stale RED release" in journal and "RED latch set" in journal

    # TTL 过期释放
    aw.latch_red(root, "s2", ["reason-b"])
    st = aw._load_audit_state(root)
    st["ts"] = time.time() - 25 * 3600
    aw._save_audit_state(root, st)
    red3, _ = aw.red_state(root, "s2")
    assert not red3


# ---------------- 干预预算 + delta 通路 ----------------

def test_intervention_budget_two_per_channel(monkeypatch):
    monkeypatch.setattr(aw, "get_config",
                        lambda: type("C", (), {"vibeweaver_loop_guard_enabled": True})())
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("conv-1", state)

    assert aw.request_interrupt("k", "conv-1", "fb1", "text")
    assert state.loop_guard_feedback == "fb1"
    state.loop_guard_feedback = ""
    assert aw.request_interrupt("k", "conv-1", "fb2", "text")
    assert not aw.request_interrupt("k", "conv-1", "fb3", "text")  # 文本通道预算 2
    assert aw.request_interrupt("k", "conv-1", "fb4", "tools")     # 工具通道独立预算

    # 无活跃 run → False
    aw.unregister_run("conv-1")
    assert not aw.request_interrupt("k", "conv-1", "fb5", "text")


def test_text_delta_path_arms_feedback(monkeypatch):
    monkeypatch.setattr(aw, "get_config",
                        lambda: type("C", (), {"vibeweaver_loop_guard_enabled": True})())
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("conv-2", state)
    # 逐段灌入行组重复文本（超过 256 字符检查阈值）
    block = "alpha beta gamma delta\nepsilon zeta eta theta\n"
    finding = None
    for _ in range(30):
        f = aw.observe_text_delta("conv-2", block)
        if f:
            finding = f
    assert finding is not None and finding["kind"] == "repeat"
    fb = aw._intervention_text(finding, "text")
    assert aw.request_interrupt("conv-2", "conv-2", fb, "text")
    assert "loop-guard" in state.loop_guard_feedback


def test_terminal_tool_observation_path(monkeypatch):
    monkeypatch.setattr(aw, "get_config",
                        lambda: type("C", (), {"vibeweaver_loop_guard_enabled": True})())
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("conv-3", state)
    finding = None
    for i in range(5):
        finding = aw.observe_tool_call("conv-3", "terminal", f"echo narration {i}")
    assert finding is not None and finding["kind"] == "noop-bash"
    assert aw.request_interrupt("conv-3", "conv-3", aw._intervention_text(finding, "tools"), "tools")
    assert "echo" in state.loop_guard_feedback


# ---------------- W12：skill.activated 消费 + C17/terminal 写观察 ----------------

def test_skill_activated_event_recorded():
    from app.services.agent_event_bus import AgentEvent
    ev = AgentEvent(type="skill.activated", session_id="sess-a", data={"name": "vibeweaver"})
    asyncio.run(aw._event_listener(ev))
    assert aw.skill_activation_for("sess-a") == "vibeweaver"


def test_c17_warns_when_active_project_written_without_skill(tmp_path, caplog, monkeypatch):
    import logging

    from app.services import vibeweaver_gate_service as gw
    ws = tmp_path / "ws"
    ws.mkdir()
    proj = ws / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "verification_log.md").write_text("- iter 1 PASS: x\n", encoding="utf-8")
    gw.remember_project_root("conv-c17", proj.resolve())
    monkeypatch.setattr(
        aw, "audit_final_output", lambda content, root: {"verdict": "skip", "reasons": []}
    )
    with caplog.at_level(logging.WARNING, logger="app.services.vibeweaver_audit_service"):
        asyncio.run(aw._audit_stop_hook(
            hs.StopContext(conversation_id="conv-c17", workspace_path=str(ws), assistant_content="")
        ))
    assert any("C17" in r.message for r in caplog.records), [r.message for r in caplog.records]
    # 二次不重复告警
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.vibeweaver_audit_service"):
        asyncio.run(aw._audit_stop_hook(
            hs.StopContext(conversation_id="conv-c17", workspace_path=str(ws), assistant_content="")
        ))
    assert not any("C17" in r.message for r in caplog.records)
    # 技能激活后即便新会话也不告警
    from app.services.agent_event_bus import AgentEvent
    asyncio.run(aw._event_listener(AgentEvent(type="skill.activated", session_id="conv-c17b", data={"name": "vibeweaver"})))
    gw.remember_project_root("conv-c17b", proj.resolve())
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.vibeweaver_audit_service"):
        asyncio.run(aw._audit_stop_hook(
            hs.StopContext(conversation_id="conv-c17b", workspace_path=str(ws), assistant_content="")
        ))
    assert not any("C17" in r.message for r in caplog.records)
    gw._RECENT_ROOTS.clear()


def test_terminal_write_observation_once_per_session(tmp_path, caplog):
    import logging

    from app.services import vibeweaver_gate_service as gw
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "verification_log.md").write_text("x\n", encoding="utf-8")
    gw.remember_project_root("conv-t", proj.resolve())
    ctx = hs.ToolHookContext(
        tool_name="terminal",
        args={"command": f"cp a.txt {proj.as_posix()}/b.txt"},
        conversation_id="conv-t",
    )
    with caplog.at_level(logging.INFO, logger="app.services.vibeweaver_audit_service"):
        asyncio.run(aw._audit_tool_after(ctx))
    assert any("bypasses the gate" in r.message for r in caplog.records), [r.message for r in caplog.records]
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.services.vibeweaver_audit_service"):
        asyncio.run(aw._audit_tool_after(ctx))
    assert not any("bypasses the gate" in r.message for r in caplog.records)
    gw._RECENT_ROOTS.clear()


# ---------------- W12 评审修复：producer/C17/RED-latch 归属 ----------------

def test_skill_view_publishes_activation_with_conversation_obj():
    from app.services.agent_event_bus import agent_event_bus
    from app.tools import skill_tools
    seen = []
    unsub = agent_event_bus.subscribe(lambda ev: seen.append(ev), name="test-cap")

    async def scenario():
        await skill_tools.skill_view({"name": "vibeweaver"}, conversation=SimpleNamespace(id="conv-live"))
        await asyncio.sleep(0.05)

    try:
        asyncio.run(scenario())
    finally:
        unsub()
    events = [e for e in seen if e.type == "skill.activated"]
    assert events and events[-1].session_id == "conv-live", [e.session_id for e in events]


def test_c17_not_suppressed_by_unrelated_skill():
    from app.services.agent_event_bus import AgentEvent
    from app.services import vibeweaver_gate_service as gw
    proj = Path("/tmp") / f"c17-proj-{time.time_ns()}"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "verification_log.md").write_text("x\n", encoding="utf-8")
    try:
        asyncio.run(aw._event_listener(AgentEvent(
            type="skill.activated", session_id="conv-o", data={"name": "some-other-skill"})))
        gw.remember_project_root("conv-o", proj.resolve())
        assert aw.skill_activation_for("conv-o") == "some-other-skill"
        # 非 vibeweaver 激活 → C17 仍应告警（名称判定）
        called = {"warn": False}
        orig = aw.logger.warning
        aw.logger.warning = lambda *a, **k: called.__setitem__("warn", True) or orig(*a, **k)
        try:
            asyncio.run(aw._audit_stop_hook(hs.StopContext(
                conversation_id="conv-o", workspace_path=str(proj), assistant_content="")))
        finally:
            aw.logger.warning = orig
        assert called["warn"] is True
    finally:
        gw._RECENT_ROOTS.clear()
        import shutil as _sh
        _sh.rmtree(proj, ignore_errors=True)


def test_red_latch_blocks_write_in_registered_subdir_project(tmp_path):
    from app.services import vibeweaver_gate_service as gw
    ws = tmp_path / "ws"
    ws.mkdir()
    proj = ws / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "verification_log.md").write_text("- iter 1 PASS: x\n", encoding="utf-8")
    gw.remember_project_root("conv-b", proj.resolve())
    aw.latch_red(proj.resolve(), "conv-b", ["probe bad"])
    ctx = hs.ToolHookContext(
        tool_name="workspace_write",
        args={"path": str(proj / "src" / "x.js")},
        conversation_id="conv-b",
        workspace_path=str(ws),
    )
    d = asyncio.run(aw._audit_tool_before(ctx))
    assert d is not None and d.block and "RED-LATCHED" in d.block
    # 证据修复路径（tests/）不封
    ctx2 = hs.ToolHookContext(
        tool_name="workspace_write",
        args={"path": str(proj / "tests" / "verification_log.md")},
        conversation_id="conv-b",
        workspace_path=str(ws),
    )
    assert asyncio.run(aw._audit_tool_before(ctx2)) is None
    gw._RECENT_ROOTS.clear()


def test_red_latch_blocks_hintless_git_call_via_registry(tmp_path):
    from app.services import vibeweaver_gate_service as gw
    ws = tmp_path / "ws"
    ws.mkdir()
    proj = ws / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "verification_log.md").write_text("- iter 1 PASS: x\n", encoding="utf-8")
    gw.remember_project_root("conv-g", proj.resolve())
    aw.latch_red(proj.resolve(), "conv-g", ["probe bad"])
    ctx = hs.ToolHookContext(
        tool_name="git",
        args={"subcommand": "commit", "message": "wip"},
        conversation_id="conv-g",
        workspace_path=str(ws),
    )
    d = asyncio.run(aw._audit_tool_before(ctx))
    assert d is not None and d.block and "RED-LATCHED" in d.block
    gw._RECENT_ROOTS.clear()
