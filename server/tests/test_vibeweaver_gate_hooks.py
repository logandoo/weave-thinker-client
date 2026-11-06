"""hook 运行时 + vibeweaver gate（P1）— A4.8 test-first。

覆盖：tool.before/after block/notes 语义与 fail-open；stop hook 预算
（每轮/每会话）与无反馈 block 视作放行；gate 的 active 判定、证据路径豁免
（tests//memory/ 写入不设门）、内联证据地板、GATE-BLOCKED 文案、停顿观察器
3× 规则、assert_artifacts.py 自动物化、VIBEWEAVER_GATE=off 逃生口。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import hook_service as hs  # noqa: E402
from app.services import vibeweaver_gate_service as gw  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_hooks():
    hs.clear_hooks()
    yield
    hs.clear_hooks()


# ---------------- hook_service ----------------

def test_tool_before_block_short_circuits():
    async def blocker(ctx):
        return hs.HookDecision(block="before says no")

    async def never(ctx):  # pragma: no cover - 短路后不应被调用
        raise AssertionError("second hook must not run after block")

    async def scenario():
        hs.register_tool_hook("before", blocker)
        hs.register_tool_hook("before", never)
        d = await hs.run_tool_hooks("before", hs.ToolHookContext(tool_name="x"))
        assert d.block == "before says no"

    asyncio.run(scenario())


def test_tool_after_notes_aggregate_and_fail_open():
    async def boom(ctx):
        raise RuntimeError("hook bug")

    async def noter(ctx):
        return hs.HookDecision(notes=["note-1"])

    async def noter2(ctx):
        return hs.HookDecision(notes=["note-2"])

    async def scenario():
        hs.register_tool_hook("after", boom)
        hs.register_tool_hook("after", noter)
        hs.register_tool_hook("after", noter2)
        d = await hs.run_tool_hooks("after", hs.ToolHookContext(tool_name="x", result="{}"))
        assert d.block is None
        assert d.notes == ["note-1", "note-2"]  # 异常钩子 fail-open 不影响后续

    asyncio.run(scenario())


def test_stop_hook_budget_per_turn_and_conversation(monkeypatch):
    async def blocker(ctx):
        return hs.StopDecision(allow=False, feedback="fix evidence", reason="red")

    async def scenario():
        hs.register_stop_hook(blocker)
        monkeypatch.setattr(
            "app.core.config.get_config",
            lambda: type("C", (), {
                "hooks_stop_max_per_turn": 2,
                "hooks_stop_max_per_conversation": 3,
            })(),
        )
        d1 = await hs.run_stop_hooks(hs.StopContext(conversation_id="c1", stop_hook_blocks_this_turn=0))
        assert not d1.allow and d1.feedback == "fix evidence"
        d2 = await hs.run_stop_hooks(hs.StopContext(conversation_id="c1", stop_hook_blocks_this_turn=1))
        assert not d2.allow  # 第 2 次仍可拦（每轮预算 2）
        d3 = await hs.run_stop_hooks(hs.StopContext(conversation_id="c1", stop_hook_blocks_this_turn=2))
        assert d3.allow  # 每轮预算耗尽 → 放行（放行不计入会话预算）
        # 换轮（计数归零）：会话预算还剩 1 次 → 第 3 次拦截
        d4 = await hs.run_stop_hooks(hs.StopContext(conversation_id="c1", stop_hook_blocks_this_turn=0))
        assert not d4.allow
        # 会话预算 3/3 已耗尽 → 此后一律放行
        d5 = await hs.run_stop_hooks(hs.StopContext(conversation_id="c1", stop_hook_blocks_this_turn=0))
        assert d5.allow and "budget" in (d5.reason or "")
        assert hs.stop_block_count("c1") == 3
        # 另一会话独立预算
        d6 = await hs.run_stop_hooks(hs.StopContext(conversation_id="c2", stop_hook_blocks_this_turn=0))
        assert not d6.allow

    asyncio.run(scenario())


def test_stop_hook_block_without_feedback_is_allow():
    async def bad(ctx):
        return hs.StopDecision(allow=False, feedback="   ", reason="no feedback")

    async def scenario():
        hs.register_stop_hook(bad)
        d = await hs.run_stop_hooks(hs.StopContext(conversation_id="c9"))
        assert d.allow

    asyncio.run(scenario())


# ---------------- vibeweaver_gate_service ----------------

def _mk_project(root: Path, with_log=True, acc_first="> cap=5  stall=3×", iters=1):
    (root / "tests").mkdir(parents=True, exist_ok=True)
    if with_log:
        log = "\n".join(f"- iter {i+1} PASS: criterion #1" for i in range(iters)) + "\n"
        (root / "tests" / "verification_log.md").write_text(log, encoding="utf-8")
    (root / "tests" / "acceptance.md").write_text(acc_first + "\n1. x\n", encoding="utf-8")


def test_find_project_root_and_evidence_path(tmp_path):
    proj = tmp_path / "proj"
    _mk_project(proj)
    sub = proj / "src" / "deep"
    sub.mkdir(parents=True)
    assert gw.find_project_root([str(sub)], bound=str(proj)) == proj.resolve()
    assert gw.find_project_root([str(tmp_path / "nope")], bound=str(proj)) is None
    assert gw.find_project_root([str(sub)]) is None  # 无 bound = fail-closed
    assert gw.is_evidence_path(proj.resolve(), str(proj / "tests" / "x.md"))
    assert gw.is_evidence_path(proj.resolve(), str(proj / "memory" / "m.md"))
    assert not gw.is_evidence_path(proj.resolve(), str(proj / "src" / "app.js"))
    assert not gw.is_evidence_path(proj.resolve(), str(proj / "src" / "tests" / "fake.py"))  # 首段锚定


def test_inline_floor_failures(tmp_path):
    proj = tmp_path / "p"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "verification_log.md").write_text("nothing here\n", encoding="utf-8")
    (proj / "tests" / "acceptance.md").write_text("wrong first line\n", encoding="utf-8")
    (proj / "tests" / "verification_log.md").write_text(
        "cites tests/shot.png\n- iter 1 PASS: ok\n", encoding="utf-8")
    fails = gw.inline_check(proj)
    text = "\n".join(fails)
    assert "cap=5" in text            # acceptance 首行缺失
    assert "shot.png" in text          # 引用截图缺失/空
    assert "COV-1" not in text         # iter 条目已存在


def test_gate_blocks_on_stub_assert_failure_and_passes_on_success(tmp_path, monkeypatch):
    proj = tmp_path / "p"
    _mk_project(proj)
    stub = proj / "tests" / "assert_artifacts.py"
    stub.write_text(
        "import sys\nprint('- verification_log.md missing iter entries (COV-1)')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gw, "get_config",
                        lambda: type("C", (), {"vibeweaver_gate_enabled": True})())

    async def scenario():
        out = await gw.gate_check_write_async(proj.resolve(), str(proj / "src" / "app.js"))
        assert out and out.get("block") and "GATE-BLOCKED" in out["block"]
        assert "verification_log" in out["block"]

        # 证据路径写入 → 豁免
        out2 = await gw.gate_check_write_async(proj.resolve(), str(proj / "tests" / "acceptance.md"))
        assert out2 is None or not out2.get("block")

        # 修好 stub → 放行（无 block）
        stub.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        out3 = await gw.gate_check_write_async(proj.resolve(), str(proj / "src" / "app.js"))
        assert out3 is None or not out3.get("block")

    asyncio.run(scenario())


def test_gate_disabled_env_escape(tmp_path, monkeypatch):
    proj = tmp_path / "p"
    _mk_project(proj)
    monkeypatch.setenv("VIBEWEAVER_GATE", "off")
    monkeypatch.setattr(gw, "get_config",
                        lambda: type("C", (), {"vibeweaver_gate_enabled": False})())

    async def scenario():
        assert await gw.gate_check_write_async(proj.resolve(), str(proj / "src" / "a.js")) is None

    asyncio.run(scenario())


def test_stall_observer_three_same_file_ops(tmp_path):
    proj = tmp_path / "p"
    _mk_project(proj, iters=1)
    assert gw.stall_observation(proj.resolve(), "src/a.py") is None
    assert gw.stall_observation(proj.resolve(), "src/a.py") is None
    msg = gw.stall_observation(proj.resolve(), "src/a.py")
    assert msg and "STALL observed" in msg
    # 新 PASS 条目后计数重置
    with (proj / "tests" / "verification_log.md").open("a", encoding="utf-8") as f:
        f.write("- iter 2 PASS: criterion #1\n")
    assert gw.stall_observation(proj.resolve(), "src/a.py") is None


def test_assert_script_materialized_from_skill(tmp_path, monkeypatch):
    proj = tmp_path / "p"
    _mk_project(proj)
    repo_root = Path(__file__).resolve().parent.parent
    monkeypatch.setattr(gw, "get_config",
                        lambda: type("C", (), {
                            "vibeweaver_gate_enabled": True,
                            "resolve_resource": lambda self, *parts, bundled_first=False:
                                repo_root.joinpath(*parts),
                        })())
    script = gw._ensure_assert_script(proj.resolve())
    assert script and script.exists()
    body = script.read_text(encoding="utf-8")
    assert "assert_artifacts" in body or "PASSES" in body


def test_gate_stop_hook_blocks_and_feeds_back(tmp_path, monkeypatch):
    proj = tmp_path / "p"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "verification_log.md").write_text("no iters\n", encoding="utf-8")
    (proj / "tests" / "acceptance.md").write_text("> cap=5  stall=3×\n", encoding="utf-8")
    monkeypatch.setattr(gw, "get_config",
                        lambda: type("C", (), {"vibeweaver_gate_enabled": True})())

    async def scenario():
        d = await gw._stop_hook(hs.StopContext(workspace_path=str(proj)))
        assert d is not None and not d.allow
        assert "GATE" in d.feedback or "拦截" in d.feedback
        assert "iter" in d.feedback

    asyncio.run(scenario())


def test_project_root_bound_rejects_poisoned_ancestor(tmp_path):
    """A4.9 C2：祖先目录带 tests/verification_log.md 不得激活执法层。"""
    install = tmp_path / "install"
    (install / "tests").mkdir(parents=True)
    (install / "tests" / "verification_log.md").write_text("- iter 1 PASS: host repo\n", encoding="utf-8")
    ws = install / "backend" / "user_workspaces" / "u1"
    ws.mkdir(parents=True)
    # 工作区自身无 vibeweaver 产物 → 必须 None（不得挂到 install 根）
    assert gw.find_project_root([str(ws)], bound=str(ws)) is None
    # 工作区有产物 → 命中工作区根（即使祖先也有）
    (ws / "tests").mkdir()
    (ws / "tests" / "verification_log.md").write_text("- iter 1 PASS: ws\n", encoding="utf-8")
    root = gw.find_project_root([str(ws)], bound=str(ws))
    assert root == ws.resolve()
    # bound 缺失 → fail-closed
    assert gw.find_project_root([str(ws)]) is None
    # 起点在 bound 之外 → None
    assert gw.find_project_root([str(install / "tests")], bound=str(ws)) is None


# ---------------- W12：资源根物化 + 无 python3 回退 ----------------

def test_ensure_assert_script_materializes_from_resource_root(tmp_path, monkeypatch):
    proj = tmp_path / "p"
    (proj / "tests").mkdir(parents=True)
    bundled = tmp_path / "bundle" / "skills" / "vibeweaver" / "scripts"
    bundled.mkdir(parents=True)
    src = bundled / "assert_artifacts.py"
    src.write_text("# canonical\n", encoding="utf-8")
    seen = {}

    class Cfg:
        def resolve_resource(self, *parts, bundled_first=False):
            seen["parts"] = parts
            seen["bundled_first"] = bundled_first
            return src

    monkeypatch.setattr(gw, "get_config", lambda: Cfg())
    out = gw._ensure_assert_script(proj)
    assert out == (proj / "tests" / "assert_artifacts.py")
    assert out.read_text(encoding="utf-8") == "# canonical\n"
    assert seen["parts"] == ("skills", "vibeweaver", "scripts", "assert_artifacts.py")
    assert seen["bundled_first"] is True  # 可执行资产：包内副本优先（防数据目录投毒）


def test_check_gate_async_inline_fallback_without_python(tmp_path, monkeypatch):
    proj = tmp_path / "p"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "verification_log.md").write_text("- iter 1 PASS: x\n", encoding="utf-8")
    monkeypatch.setattr(gw.shutil, "which", lambda _name: None)
    monkeypatch.setattr(gw, "get_config", lambda: type("C", (), {})())
    out = asyncio.run(gw.check_gate_async(proj))
    assert out and out.get("inline")
    assert any("acceptance" in m for m in out["blocking"])


# ---------------- W12 评审修复：注册表/hint 优先 ----------------

def test_stop_hook_uses_recent_subdir_project_root(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    proj = ws / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "verification_log.md").write_text("no iters\n", encoding="utf-8")
    monkeypatch.setattr(gw, "get_config", lambda: type("C", (), {"vibeweaver_gate_enabled": True})())
    gw.remember_project_root("conv-sub", proj.resolve())

    async def scenario():
        d = await gw._stop_hook(hs.StopContext(conversation_id="conv-sub", workspace_path=str(ws)))
        assert d is not None and not d.allow
    asyncio.run(scenario())
    gw._RECENT_ROOTS.clear()


def test_recent_project_root_invalidated_when_marker_removed(tmp_path):
    proj = tmp_path / "p"
    (proj / "tests").mkdir(parents=True)
    marker = proj / "tests" / "verification_log.md"
    marker.write_text("x", encoding="utf-8")
    gw.remember_project_root("k1", proj.resolve())
    assert gw.recent_project_root("k1") == proj.resolve()
    marker.unlink()
    assert gw.recent_project_root("k1") is None


def test_tool_hint_prefers_own_project_over_registry(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    a, b = ws / "a", ws / "b"
    for p in (a, b):
        (p / "tests").mkdir(parents=True)
        (p / "tests" / "verification_log.md").write_text("x", encoding="utf-8")
    gw.remember_project_root("conv-x", a.resolve())
    root = gw.resolve_session_root(
        str(ws), "conv-x", starts=[str(b / "src" / "f.js")], prefer_hints=True
    )
    assert root == b.resolve()
    assert gw.recent_project_root("conv-x") == b.resolve()  # 注册表更新为本次归属
    gw._RECENT_ROOTS.clear()


def test_relative_hint_resolves_against_bound(tmp_path):
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "verification_log.md").write_text("x", encoding="utf-8")
    root = gw.resolve_session_root(str(ws), "k", starts=["src/a.js"], prefer_hints=True)
    assert root == ws.resolve()


def test_hintless_call_falls_back_to_registry(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    proj = ws / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "verification_log.md").write_text("x", encoding="utf-8")
    gw.remember_project_root("k2", proj.resolve())
    # git 类写工具无 file_path/path 提示 → 回退注册表（复评审 N1）
    root = gw.resolve_session_root(str(ws), "k2", starts=[None, None], prefer_hints=True)
    assert root == proj.resolve()
    gw._RECENT_ROOTS.clear()


# ---------------- W12 复审修复（A4.9 A-I1/A-I3）----------------

def test_hints_unresolved_fallthrough_returns_none_not_registry(tmp_path):
    """复审 A-I1：给了可解析 hint 但不属于任何 active 项目 → None（不设门），
    不得回退注册表把 B 项目的首次写入按 A 项目的 RED-latch 误封。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    proj_a = ws / "a"
    (proj_a / "tests").mkdir(parents=True)
    (proj_a / "tests" / "verification_log.md").write_text("x", encoding="utf-8")
    new_b = ws / "b"  # 尚未成为 active 项目（无 tests/verification_log.md）
    new_b.mkdir()
    gw.remember_project_root("conv-y", proj_a.resolve())
    try:
        root = gw.resolve_session_root(
            str(ws), "conv-y", starts=[str(new_b / "tests" / "acceptance.md")],
            prefer_hints=True,
        )
        assert root is None, "hint 可解析但无项目归属时必须 None（注册表仅限无提示兜底）"
    finally:
        gw._RECENT_ROOTS.clear()


def test_is_evidence_path_relative_to_root_not_cwd(tmp_path, monkeypatch):
    """复审 A-I3：tool schema 允许工作区相对路径；is_evidence_path 以进程 CWD
    解析相对路径会把项目的 tests/ 证据写误判为非证据 → RED-latch 误封修复路径。"""
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)  # CWD 不在项目内
    assert gw.is_evidence_path(proj, "tests/verification_log.md") is True
    assert gw.is_evidence_path(proj, "src/main.py") is False


# 复审 B-M3：注册表跨用例污染防护（断言失败早退时手动 clear 被跳过）
import pytest as _pytest


@_pytest.fixture(autouse=True)
def _clear_recent_roots_registry():
    yield
    gw._RECENT_ROOTS.clear()
