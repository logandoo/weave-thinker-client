"""最新 vibeweaver 硬 hook 对接（2026-10-13 计划 W1-W4）— A4.8 判别测试。

W1：D1 loop-guard 全形态 + reasoning 双通道 + ceiling + re-arm；D2 stall latch；
D8 遥测。fixture 形状对齐 SKILLS 仓 scripts/audit_selftest.mjs 的 T24k/T24l/T27。
每项三型：触发 / 不触发 / 预算或去重。
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
from app.services import vibeweaver_gate_service as gw  # noqa: E402
from app.services import vibeweaver_state as vst  # noqa: E402
from app.core.config import Config  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    hs.clear_hooks()
    aw._observations.clear()
    aw._active_runs.clear()
    gw._RECENT_ROOTS.clear()
    yield
    hs.clear_hooks()
    aw._observations.clear()
    aw._active_runs.clear()
    gw._RECENT_ROOTS.clear()


def _lg_on(monkeypatch):
    monkeypatch.setattr(
        aw, "get_config",
        lambda: type("C", (), {"vibeweaver_loop_guard_enabled": True})(),
    )


def wall(n: int) -> str:
    return "Stop it. " * n


def _letters26(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(97 + r) + s
    return s


def prose(n: int) -> str:
    return "\n".join(
        f"prose line {_letters26(i)} assorted words and more filler tail" for i in range(1, n + 1)
    )


# ================= D1：字符路径（尾噪归一 / 标点墙 / gate256） =================

@pytest.mark.parametrize("noise", ["\n", " ", "\n\n", "\u00a0", "\u200b", "\r\n"])
def test_t24k_tail_noise_single_line_runaway(noise):
    f = aw.loop_finding(wall(300) + noise)
    assert f and f["kind"] == "repeat", (noise, f)


def test_t24k_inline_runaway_in_newline_rich_context():
    f = aw.loop_finding("a line here\nb line there\nc line x\nd line y\n" + wall(50) + "\n")
    assert f and f["kind"] == "repeat"


def test_t24l_punct_walls():
    assert aw.loop_finding("!" * 4000)
    assert aw.loop_finding("!" * 2100)
    assert aw.loop_finding("!!!" * 800)
    assert aw.loop_finding("!" * 2048)  # 边界精确命中


def test_t24l_punct_false_positive_bars():
    # 大文档 + 128 字符 punct 束（墙 <2048）→ 干净
    assert aw.loop_finding("report data " + "x" * 2500 + " " + "=" * 128) is None
    # 短分隔线 ×3 → 干净
    assert aw.loop_finding("----------\n" * 3) is None


def test_t24k_gate256_boundary():
    prefix = "a line here\nb line there\nc line x\nd line y\n"
    assert aw.loop_finding(prefix + wall(28) + "\n") is None          # 252 字符 < gate
    assert aw.loop_finding(prefix + wall(29) + "\n")                  # 261 字符 ≥ gate
    assert aw.loop_finding(prefix + "abcde" * 51 + "\n") is None      # 255 < 256
    assert aw.loop_finding(prefix + "abcdefgh" * 32 + "\n")           # 256 = gate


# ================= D1：counter 墙 =================

def test_t24l_counter_wall_fires():
    backlog = "\n".join(f"The backlog item {i} is pending review." for i in range(200))
    f = aw.loop_finding(backlog)
    assert f and "counter run" in f["pattern"], f
    queue = "\n".join(f"queue entry {i} awaits triage" for i in range(20)) + "\nDone."
    f2 = aw.loop_finding(queue)
    assert f2 and "counter run" in f2["pattern"], f2


def test_t24l_counter_wall_boundary_16():
    assert aw.loop_finding("\n".join(f"case {i + 1} executed" for i in range(15))) is None
    assert aw.loop_finding("\n".join(f"case {i + 1} executed" for i in range(16)))


def test_t24l_counter_wall_false_positive_bars():
    ts = "\n".join(
        f"2026-10-10 12:00:{str(i).zfill(2)} INFO job completed" for i in range(30)
    )
    assert aw.loop_finding(ts) is None  # 只有末段 tick → 严格 +1 排除
    paged = "\n".join(f"page {i + 1} of 30 rendered" for i in range(30))
    assert aw.loop_finding(paged) is None  # M 恒定
    short = "\n".join(f"test{i + 1} passed" for i in range(5))
    assert aw.loop_finding(short) is None


# ================= D1：reasoning 通道 + 预算隔离 =================

def test_t24l_reasoning_channel_and_independent_budget(monkeypatch):
    _lg_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("conv-r", state)
    # reasoning 墙命中
    f = aw.observe_text_delta("conv-r", wall(300), channel="reasoning", msg_id="m1")
    assert f and f["kind"] == "repeat"
    assert aw.request_interrupt("conv-r", "conv-r", aw._intervention_text(f, "reasoning"), "reasoning")
    # 新 turn 再退化 → 第二次（预算 2）
    f = aw.observe_text_delta("conv-r", wall(300), channel="reasoning", msg_id="m2")
    assert f, "新 message 边界应重武装"
    assert aw.request_interrupt("conv-r", "conv-r", aw._intervention_text(f, "reasoning"), "reasoning")
    # 第三 episode → 预算耗尽 log-only
    f = aw.observe_text_delta("conv-r", wall(300), channel="reasoning", msg_id="m3")
    assert f
    assert not aw.request_interrupt("conv-r", "conv-r", aw._intervention_text(f, "reasoning"), "reasoning")
    # text 通道独立预算（不被 reasoning 耗尽）
    f2 = aw.observe_text_delta("conv-r", wall(300), channel="content", msg_id="m4")
    assert f2 and aw.request_interrupt("conv-r", "conv-r", aw._intervention_text(f2, "text"), "text")


def test_delta_latch_no_thrash_on_same_turn(monkeypatch):
    _lg_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("conv-nt", state)
    f = aw.observe_text_delta("conv-nt", wall(300), channel="content", msg_id="m1")
    assert f
    assert aw.request_interrupt("conv-nt", "conv-nt", "fb", "text")
    # 同 turn 尾部仍退化 → 闩锁抑制（no-thrash）
    assert aw.observe_text_delta("conv-nt", wall(300), channel="content", msg_id="m1") is None
    # 新 turn → 重武装
    assert aw.observe_text_delta("conv-nt", wall(300), channel="content", msg_id="m2")
    assert aw.request_interrupt("conv-nt", "conv-nt", "fb2", "text")


def test_clean_observation_rearms(monkeypatch):
    _lg_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("conv-clean", state)
    f = aw.observe_text_delta("conv-clean", wall(300), channel="content", msg_id="m1")
    assert f and aw.request_interrupt("conv-clean", "conv-clean", "fb", "text")
    # 干净文本 → 观察无 finding 且重武装（prose 各行动态变化，无周期）
    assert aw.observe_text_delta(
        "conv-clean", prose(40), channel="content", msg_id="m1",
    ) is None
    # 重武装后同 turn 再退化 → 直接命中（无需新 turn）
    assert aw.observe_text_delta("conv-clean", wall(300), channel="content", msg_id="m1")


# ================= D1：per-turn ceiling =================

def test_t24l_turn_ceiling_and_msg_reset(monkeypatch):
    _lg_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("conv-u", state)
    p_under = prose(900)   # ~45KB < 64KB
    f = aw.observe_text_delta("conv-u", p_under, channel="content", msg_id="m1")
    assert f is None, "45KB turn 不应触发 ceiling"
    f = aw.observe_text_delta("conv-u", p_under, channel="content", msg_id="m2")
    assert f is None, "message 边界重置 turnChars：两个 45KB turn 均干净"


def test_t24l_turn_ceiling_fires_same_turn(monkeypatch):
    _lg_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("conv-u2", state)
    p = prose(900)
    aw.observe_text_delta("conv-u2", p, channel="content", msg_id="m1")
    f = aw.observe_text_delta("conv-u2", p, channel="content", msg_id="m1")
    assert f and f["kind"] == "runaway" and "65536" in f["pattern"]


def test_t24l_turn_ceiling_without_msg_ids(monkeypatch):
    _lg_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("conv-u3", state)
    f = aw.observe_text_delta("conv-u3", prose(2000), channel="content")
    assert f and f["kind"] == "runaway"


def test_t24l_ceiling_rescues_latched_wall(monkeypatch):
    _lg_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("conv-u4", state)
    f = aw.observe_text_delta("conv-u4", wall(300), channel="content", msg_id="m1")
    assert f and aw.request_interrupt("conv-u4", "conv-u4", "fb", "text")
    # 同 turn 闩锁下继续膨胀越过下一个 64KB 窗 → runaway 救援
    f2 = aw.observe_text_delta("conv-u4", prose(1600), channel="content", msg_id="m1")
    assert f2 and f2["kind"] == "runaway"


def test_runaway_intervention_text_distinct():
    txt = aw._intervention_text({"kind": "runaway", "pattern": "turn output exceeded 65536 bytes"}, "text")
    assert "runaway" in txt.lower() and "65536" in txt


# ================= D2：stall latch =================

def _mk_proj(tmp_path: Path, iters: int = 1) -> Path:
    proj = tmp_path / "p"
    (proj / "tests").mkdir(parents=True)
    log = "\n".join(f"- iter {i + 1} PASS: criterion #1" for i in range(iters)) + "\n"
    (proj / "tests" / "verification_log.md").write_text(log, encoding="utf-8")
    return proj


def test_d2_stall_latch_once_per_signature_and_rearm(tmp_path):
    proj = _mk_proj(tmp_path, iters=1)
    assert gw.stall_observation(proj, "src/a.py") is None
    assert gw.stall_observation(proj, "src/a.py") is None
    msg = gw.stall_observation(proj, "src/a.py")
    assert msg and "STALL observed" in msg
    # 同签名（同文件同 pass 数）继续写 → 闩锁抑制
    assert gw.stall_observation(proj, "src/a.py") is None
    assert gw.stall_observation(proj, "src/a.py") is None
    # 新 PASS 落地 → 签名变化 → 重武装（3 次新计数后再次报告）
    with (proj / "tests" / "verification_log.md").open("a", encoding="utf-8") as f:
        f.write("- iter 2 PASS: criterion #1\n")
    assert gw.stall_observation(proj, "src/a.py") is None
    assert gw.stall_observation(proj, "src/a.py") is None
    msg2 = gw.stall_observation(proj, "src/a.py")
    assert msg2 and "STALL observed" in msg2
    st = json.loads((proj / ".vibeweaver" / "state.json").read_text(encoding="utf-8"))
    assert st["stall"]["firedKey"] == "src/a.py@2"


def test_d2_stall_preserves_unknown_state_fields(tmp_path):
    proj = _mk_proj(tmp_path)
    sd = proj / ".vibeweaver"
    sd.mkdir()
    (sd / "state.json").write_text(json.dumps({"unknownKey": 42, "ops": []}), encoding="utf-8")
    for _ in range(3):
        gw.stall_observation(proj, "src/b.py")
    st = json.loads((sd / "state.json").read_text(encoding="utf-8"))
    assert st.get("unknownKey") == 42
    assert "stall" in st


# ================= D8：遥测 =================

def test_d8_loop_guard_telemetry_bounded(tmp_path, monkeypatch):
    proj = _mk_proj(tmp_path)
    _lg_on(monkeypatch)
    gw.remember_project_root("conv-tel", proj.resolve())
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("conv-tel", state)
    f = aw.observe_text_delta("conv-tel", wall(300), channel="content", msg_id="m1")
    assert f and aw.request_interrupt("conv-tel", "conv-tel", "fb", "text")
    st = json.loads((proj / ".vibeweaver" / "state.json").read_text(encoding="utf-8"))
    assert st["loopGuard"]["interventions"]["text"] == 1
    assert st["loopGuard"]["last"]["channel"] == "text"
    # 预算耗尽 → suppressed 计数（有界、不炸）
    aw.request_interrupt("conv-tel", "conv-tel", "fb", "text")
    aw.request_interrupt("conv-tel", "conv-tel", "fb", "text")
    st2 = json.loads((proj / ".vibeweaver" / "state.json").read_text(encoding="utf-8"))
    assert st2["loopGuard"]["suppressed"] >= 1


def test_d8_memory_delivery_and_cue_stats_helpers(tmp_path):
    proj = _mk_proj(tmp_path)
    vst.record_memory_delivery(proj, "compactContext")
    vst.record_memory_delivery(proj, "compactContext")
    vst.record_memory_delivery(proj, "sessionCue")
    st = vst.load_state(proj)
    assert st["memoryDeliveries"] == {"compactContext": 2, "sessionCue": 1}
    vst.record_cue_fires(proj, ["memory/alpha.md", "memory/beta.md"])
    vst.record_cue_fires(proj, ["memory/alpha.md"])
    stats = json.loads(vst.cue_stats_path(proj).read_text(encoding="utf-8"))
    assert stats["alpha.md"] == 2 and stats["beta.md"] == 1
    # 未知字段保留（state 读改写不动其它键）
    st2 = vst.load_state(proj)
    assert st2["memoryDeliveries"]["compactContext"] == 2


# ================= D3：stop-guard 检测器（T27 形状） =================

from app.services import vibeweaver_stop_guard as sg  # noqa: E402


@pytest.mark.parametrize("text,want", [
    ("工作量太大了，上下文快耗尽了，我们先到这里吧。", True),
    ("由于上下文已耗尽，我无法继续编码。", True),
    ("I'm running out of context — stopping here.", True),
    ("The context window is almost full, let me wrap up.", True),
    ("We've hit the context limit, so I'll stop here.", True),
    ("接下来修改 form.ts 并跑测试。", False),
    ("The context of this function is complex.", False),
    ("That quote is out of context — it means something else.", False),
    ("The context is full of useful details.", False),
    ("Added detector tests: T27a1 上下文快耗尽 → hit; covered running out of context too.", False),
    ('用户说"上下文快耗尽了"，但我们会继续。', False),
    ("注意：上下文快耗尽是重点监控对象。接下来还有三件事要做：第一……第二……第三……我们还要继续验证很多内容。", False),
    ("上下文满了，只能先到这里。", True),
    ("The context is 95% full, stopping.", True),
    ("注意：上下文快耗尽是误报重点。接下来 wrap up the component refactor，还有很多工作。", False),
    ("重点监控模式：上下文快耗尽", False),
])
def test_d3_stop_euphemism_fixtures(text, want):
    got = sg.stop_euphemism(text)
    assert bool(got) is want, (text, got)


def test_d3_stop_guard_decision_whitelist_and_debt():
    trig = {"kind": "euphemism", "match": "上下文快耗尽"}
    base = dict(trigger=trig, task_active=True)
    assert sg.stop_guard_decision(trigger=None, task_active=True)["action"] == "ignore"
    assert sg.stop_guard_decision(trigger=trig, task_active=False)["action"] == "ignore"
    assert sg.stop_guard_decision(**base, paused_state=True)["action"] == "ignore"
    assert sg.stop_guard_decision(**base, class_e=True)["action"] == "ignore"
    assert sg.stop_guard_decision(**base, gate_marker=True)["action"] == "ignore"
    assert sg.stop_guard_decision(**base, user_stop=True)["action"] == "ignore"
    assert sg.stop_guard_decision(**base, tail_question=True)["action"] == "ignore"
    assert sg.stop_guard_decision(**base, telemetry=True)["action"] == "prompt"
    d = sg.stop_guard_decision(**base, telemetry=False)
    assert d["action"] == "debt" and d["why"] == "no-telemetry"


def test_d3_stop_guard_prompt_contract():
    p = sg.stop_guard_prompt("euphemism")
    assert "ALL-PASS" in p and "PAUSED" in p and "Coverage" in p
    assert "working_note" in p
    assert "%" not in p, "无 fill 版文案不得含填充率"
    t = sg.stop_guard_prompt("truncation")
    assert "truncat" in t.lower() or "length" in t.lower()


def _mk_stop_guard_proj(tmp_path: Path) -> Path:
    proj = tmp_path / "p"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "verification_log.md").write_text(
        "- iter 1 FAIL: criterion #1 | diagnosis: d | changed: a.ts\n", encoding="utf-8"
    )
    (proj / "tests" / "acceptance.md").write_text("> cap=5  stall=3×\n1. done\n", encoding="utf-8")
    return proj


def _sg_ctx(proj: Path, content: str, conv: str = "c-sg", user_text: str = "继续改代码") -> hs.StopContext:
    return hs.StopContext(
        conversation_id=conv,
        workspace_path=str(proj),
        assistant_content=content,
        had_tool_calls=True,
        last_user_text=user_text,
    )


def test_d3_stop_guard_hook_flow_budget_and_debt(tmp_path, monkeypatch):
    sg.reset_state()
    proj = _mk_stop_guard_proj(tmp_path)
    monkeypatch.setattr(sg, "get_config", lambda: type("C", (), {"vibeweaver_stop_guard_enabled": True})())
    eup1 = "试了几次都不行，上下文快耗尽了，先到这里吧。"
    eup2 = "还是不行，上下文真的快耗尽了。"
    eup3 = "上下文耗尽了。"
    d1 = asyncio.run(sg._stop_guard_hook(_sg_ctx(proj, eup1)))
    assert d1 is not None and not d1.allow and "ALL-PASS" in d1.feedback
    # 同 episode 重复 idle → no-thrash
    assert asyncio.run(sg._stop_guard_hook(_sg_ctx(proj, eup1))) is None
    # 第二 episode → 第二次干预（预算 2）
    d2 = asyncio.run(sg._stop_guard_hook(_sg_ctx(proj, eup2)))
    assert d2 is not None and not d2.allow
    # 第三 episode → 预算耗尽 → debt 落盘 + gate_audit 汇入 + 遥测
    assert asyncio.run(sg._stop_guard_hook(_sg_ctx(proj, eup3))) is None
    debts = vst.load_stop_debt(proj)
    assert len(debts) == 1 and debts[0]["kind"] == "euphemism"
    journal = (proj / "tests" / "gate_audit.md").read_text(encoding="utf-8")
    assert "stop-debt" in journal
    st = vst.load_state(proj)
    assert st["stopGuard"]["interventions"] == 2 and st["stopGuard"]["debt"] == 1
    # episode 去重：同触发重复 idle 不重复记债
    asyncio.run(sg._stop_guard_hook(_sg_ctx(proj, eup3)))
    assert len(vst.load_stop_debt(proj)) == 1


def test_d3_stop_guard_hook_whitelists(tmp_path, monkeypatch):
    sg.reset_state()
    monkeypatch.setattr(sg, "get_config", lambda: type("C", (), {"vibeweaver_stop_guard_enabled": True})())
    eup = "上下文快耗尽了，先停一下。"
    n = [0]

    def _fresh(conv: str) -> Path:
        n[0] += 1
        proj = _mk_stop_guard_proj(tmp_path / f"case{n[0]}")
        # 真实生命周期：会话开始锚在任何回合写入之前建立（评审 B-C1）
        sg.touch_session(conv)
        time.sleep(0.02)
        return proj

    # paused_state 白名单（本会话写入 → 豁免）
    p1 = _fresh("c-p1")
    (p1 / "tests" / "paused_state.md").write_text("paused", encoding="utf-8")
    assert asyncio.run(sg._stop_guard_hook(_sg_ctx(p1, eup, conv="c-p1"))) is None
    # 会话锚之前的存量 paused_state 不豁免（历史免疫防护）
    p1b = _mk_stop_guard_proj(tmp_path / "case-p1b")
    (p1b / "tests" / "paused_state.md").write_text("stale", encoding="utf-8")
    time.sleep(0.02)
    sg.touch_session("c-p1b")
    d_stale = asyncio.run(sg._stop_guard_hook(_sg_ctx(p1b, eup, conv="c-p1b")))
    assert d_stale is not None and not d_stale.allow, "存量 paused_state 不得豁免本会话"
    # Class-E 白名单
    p2 = _fresh("c-p2")
    (p2 / "tests" / "decisions.md").write_text("D-1 | trigger: Class-E stop | deployed", encoding="utf-8")
    assert asyncio.run(sg._stop_guard_hook(_sg_ctx(p2, eup, conv="c-p2"))) is None
    # gate 标记（同一消息；携带终止意图的伪停声明）
    p3 = _fresh("c-p3")
    assert asyncio.run(sg._stop_guard_hook(
        _sg_ctx(p3, "上下文快耗尽了，先到这里吧。\n[Verification Gate] Verifier: direct-read", conv="c-p3")
    )) is None
    # 用户停令
    p4 = _fresh("c-p4")
    assert asyncio.run(sg._stop_guard_hook(
        _sg_ctx(p4, eup, conv="c-p4", user_text="先停一下，我来看看")
    )) is None
    # 否定式用户消息不是停令（正则有命中但被否定排除）→ 照常干预
    p5 = _fresh("c-p5")
    d = asyncio.run(sg._stop_guard_hook(
        _sg_ctx(p5, eup, conv="c-p5", user_text="不要停下来，继续做完")
    ))
    assert d is not None and not d.allow
    # 末句问句白名单
    p6 = _fresh("c-p6")
    assert asyncio.run(sg._stop_guard_hook(
        _sg_ctx(p6, eup + "是否继续？", conv="c-p6")
    )) is None


def test_d3_stop_guard_escape_off(tmp_path, monkeypatch):
    sg.reset_state()
    proj = _mk_stop_guard_proj(tmp_path)
    monkeypatch.setattr(sg, "get_config", lambda: type("C", (), {"vibeweaver_stop_guard_enabled": False})())
    assert asyncio.run(sg._stop_guard_hook(_sg_ctx(proj, "上下文快耗尽了。"))) is None


# ================= D6：B12 final-run 锚点 =================

def test_d6_b12_final_run_anchor(tmp_path):
    root = tmp_path / "p"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "assert_artifacts.py").write_text("print(1)\n", encoding="utf-8")
    gate = (
        "[Verification Gate] Verifier: direct-read | Lane: M | Class: CODE | "
        "HARD-GATE-1: NO-TEST-NO-DONE=pass | HARD-GATE-2: SCRIPT-ONLY=pass\n"
        "| # | Problem | Research Sources (exa MCP / Context7) | Chosen Approach & Why "
        "| Files Changed | What Changed | Verification Evidence (Screenshot / Log) | Commit |\n"
    )
    (root / "tests" / "verification_log.md").write_text(
        "## Block A\n- iter 1 PASS: c1\n", encoding="utf-8"
    )
    bad = aw.audit_final_output(gate, root)
    assert bad["verdict"] == "bad" and any("B12" in r for r in bad["reasons"]), bad
    # 当前块补 final-run → ok
    (root / "tests" / "verification_log.md").write_text(
        "## Block A\n- iter 1 PASS: c1\n- final-run: --final — pass=47/fail=0\n"
        "- assert-run: assert_artifacts.py: pass=47/fail=0\n", encoding="utf-8"
    )
    ok = aw.audit_final_output(gate, root)
    assert ok["verdict"] == "ok", ok
    # 旧块 final-run 不豁免当前块（作用域=最后 `## ` 块）
    (root / "tests" / "verification_log.md").write_text(
        "## Block A\n- final-run: --final — pass=1/fail=0\n"
        "## Block B\n- iter 2 PASS: c2\n", encoding="utf-8"
    )
    bad2 = aw.audit_final_output(gate, root)
    assert bad2["verdict"] == "bad" and any("B12" in r for r in bad2["reasons"]), bad2


# ================= D4：投递 / 快照 / 首触 =================

from app.services import vibeweaver_delivery_service as dv  # noqa: E402
from app.services.context_compressor import ContextCompressor, build_summary_prompt  # noqa: E402


def _mk_active(tmp_path: Path, name: str = "p") -> Path:
    proj = tmp_path / name
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "verification_log.md").write_text(
        "- iter 1 PASS: c1\n- final-run: --final — pass=1/fail=0\n", encoding="utf-8"
    )
    (proj / "tests" / "acceptance.md").write_text("> cap=5  stall=3×\n1. x\n", encoding="utf-8")
    return proj


def test_d4_snapshot_and_three_branches(tmp_path):
    dv.reset_state()
    proj = _mk_active(tmp_path)
    note = "# Working Note\n\n## Confirmed\n- A is true\n\n## Next\n1. do B\n"
    (proj / "tests" / "working_note.md").write_text(note, encoding="utf-8")
    b1 = dv.compact_state_bundle(proj)
    assert "WORKING-STATE" in b1 and "A is true" in b1 and "Resume rule" in b1
    snap = json.loads((proj / ".vibeweaver" / "memory-snapshot.json").read_text(encoding="utf-8"))
    assert snap["text"].startswith("# Working Note") and len(snap["text"]) <= 4096
    # 删源 → 快照权威分支
    (proj / "tests" / "working_note.md").unlink()
    b2 = dv.compact_state_bundle(proj)
    assert "harness snapshot" in b2 and "A is true" in b2 and "no longer present" in b2
    # 无 note 无快照 → 创建指引分支
    proj2 = _mk_active(tmp_path, "p2")
    b3 = dv.compact_state_bundle(proj2)
    assert "COMPACT-RESUME" in b3 and "CREATE tests/working_note.md" in b3
    assert "cap=5" in b3  # acceptance 首行


def test_d4_compact_delivery_count_and_pending(tmp_path):
    dv.reset_state()
    proj = _mk_active(tmp_path)
    (proj / "tests" / "working_note.md").write_text("ledger text", encoding="utf-8")
    bundle = dv.prepare_compact_delivery(str(proj), "convA")
    assert "ledger text" in bundle
    st = vst.load_state(proj)
    assert st["memoryDeliveries"]["compactContext"] == 1
    # 下一轮一次性消费
    got = dv.consume_pending_resume(proj, "convA")
    assert got and "ledger text" in got
    assert dv.consume_pending_resume(proj, "convA") is None  # 消费即清
    st2 = vst.load_state(proj)
    assert st2["memoryDeliveries"]["compactResume"] == 1
    # 非 active 根 → 不投递不计数
    plain = tmp_path / "plain"
    plain.mkdir()
    assert dv.prepare_compact_delivery(str(plain), "c") == ""
    assert "memoryDeliveries" not in vst.load_state(plain)


def test_d4_compressor_delivery_integration(tmp_path):
    dv.reset_state()
    proj = _mk_active(tmp_path)
    (proj / "tests" / "working_note.md").write_text("ledger-in-compressor", encoding="utf-8")
    c = ContextCompressor(quiet=True)
    c.delivery_root = str(proj)
    c.delivery_conv_id = "convC"
    seen = {}

    async def _fake_summary(middle, focus_topic=None, delivery_context=""):
        seen["delivery"] = delivery_context
        return "SUMMARY-TEXT"

    c._generate_summary_async = _fake_summary  # type: ignore[method-assign]
    msgs = [{"role": "user", "content": f"message {i} " + "x" * 80} for i in range(12)]
    out = asyncio.run(c.compress_async(msgs))
    assert any(m.get("_ephemeral") == "vibeweaver_compact_resume" for m in out), out
    assert "ledger-in-compressor" in seen["delivery"]
    st = vst.load_state(proj)
    assert st["memoryDeliveries"]["compactContext"] == 1
    # 摘要 prompt 注入契约
    p = build_summary_prompt("turns-x", None, 100, delivery_context="LEDGER-X")
    assert "HARNESS-DELIVERED TASK STATE" in p and "LEDGER-X" in p
    assert "HARNESS-DELIVERED" not in build_summary_prompt("t", None, 100)


def test_d4_first_touch_cue_once_per_session(tmp_path):
    dv.reset_state()
    proj = _mk_active(tmp_path)
    (proj / "tests" / "working_note.md").write_text("note", encoding="utf-8")
    n1 = dv.first_touch_cue(proj, "convA")
    assert n1 and "working_note.md exists" in n1
    assert dv.first_touch_cue(proj, "convA") is None  # 每会话一次
    n2 = dv.first_touch_cue(proj, "convB")
    assert n2 and "convB" not in n2
    st = vst.load_state(proj)
    assert st["memoryDeliveries"]["sessionCue"] == 2
    assert (proj / ".vibeweaver" / "memory-snapshot.json").exists()  # 快照先行


def test_d4_first_touch_cue_create_instruction(tmp_path):
    dv.reset_state()
    proj = _mk_active(tmp_path)
    (proj / "tests" / "working_note.md").unlink(missing_ok=True)
    n = dv.first_touch_cue(proj, "convX")
    assert n and "create it" in n


# ================= D5：cue triggers =================

def test_d5_safe_glob_bounds():
    assert dv.safe_glob("src/**")
    assert dv.safe_glob("**/api.py")
    assert not dv.safe_glob("**/api/*.py")         # 3 个总星号 > 上游 cap=2
    assert not dv.safe_glob("**/**/**")            # >2 个 **
    assert not dv.safe_glob("x" * 65)              # 超长（上游 cap=64）
    assert not dv.safe_glob("src/\x01evil/**")     # 控制字符
    assert not dv.safe_glob("")
    assert not dv.safe_glob(None)


def test_d5_parse_triggers_forms():
    assert dv.parse_triggers('triggers: ["a/**", "b/**"]') == ["a/**", "b/**"]
    assert dv.parse_triggers('triggers:\n  - "a/**"\n  - b/**\nstatus: ⏳') == ["a/**", "b/**"]
    assert dv.parse_triggers("triggers: src/auth/**\ntype: fix") == ["src/auth/**"]
    assert dv.parse_triggers("type: fix\nstatus: ok") == []


def test_d5_cue_notes_match_limit_and_memory_exclusion(tmp_path):
    proj = _mk_active(tmp_path)
    mem = proj / "memory"
    mem.mkdir()
    for name, trig in [("a.md", "src/**"), ("b.md", "src/**"), ("c.md", "src/**"), ("d.md", "src/**")]:
        (mem / name).write_text(
            f"---\ntype: fix\nstatus: ⏳\ntriggers: [\"{trig}\"]\n---\n\n# Topic {name}\n",
            encoding="utf-8",
        )
    (mem / "MEMORY.md").write_text(
        "---\ntriggers: [\"src/**\"]\n---\n# Index\n", encoding="utf-8"
    )
    (mem / "nomatch.md").write_text(
        "---\ntriggers: [\"other/**\"]\n---\n# Other\n", encoding="utf-8"
    )
    notes = dv.cue_notes(proj, str(proj / "src" / "app.py"))
    assert len(notes) == 3, notes  # ≤3 上限；MEMORY.md 排除
    assert all("memory/" in n and "src/app.py" not in n for n in notes)
    stats = json.loads(vst.cue_stats_path(proj).read_text(encoding="utf-8"))
    assert sum(stats.values()) == 3 and "MEMORY.md" not in stats
    # memory/ 自身写入不触发
    assert dv.cue_notes(proj, str(mem / "a.md")) == []
    # 不命中 → 空
    assert dv.cue_notes(proj, str(proj / "docs" / "readme.md")) == []


def test_d4_d5_gate_tool_after_integration(tmp_path, monkeypatch):
    dv.reset_state()
    proj = _mk_active(tmp_path)
    (proj / "memory").mkdir()
    (proj / "memory" / "topic.md").write_text(
        "---\ntype: fix\nstatus: ✅\ntriggers: [\"src/**\"]\n---\n\n# 关键主题\n", encoding="utf-8"
    )
    (proj / "tests" / "working_note.md").write_text("gate-ledger", encoding="utf-8")
    (proj / "src").mkdir()
    monkeypatch.setattr(gw, "get_config", lambda: type("C", (), {"vibeweaver_gate_enabled": True})())

    def _run(fp: str):
        ctx = hs.ToolHookContext(
            tool_name="workspace_write",
            args={"file_path": fp},
            conversation_id="c-int",
            workspace_path=str(proj),
        )
        return asyncio.run(gw._tool_after_hook(ctx))

    d1 = _run(str(proj / "src" / "app.py"))
    assert d1 is not None
    joined = "\n".join(d1.notes)
    assert "vibeweaver-cue" in joined and "关键主题" in joined
    assert "working_note.md exists" in joined  # 首触
    # 第二次写入：cue 仍投递（每次编辑），首触不再出现
    d2 = _run(str(proj / "src" / "other.py"))
    joined2 = "\n".join(d2.notes) if d2 else ""
    assert "vibeweaver-cue" in joined2
    assert "working_note.md exists" not in joined2
    # working_note 写入 → 快照刷新
    (proj / "tests" / "working_note.md").write_text("gate-ledger-v2", encoding="utf-8")
    _run(str(proj / "tests" / "working_note.md"))
    snap = json.loads((proj / ".vibeweaver" / "memory-snapshot.json").read_text(encoding="utf-8"))
    assert "gate-ledger-v2" in snap["text"]


# ================= D7：C8 禁止裸命令 =================

def test_d7_forbidden_raw_matrix():
    for c in [
        "npm run build", "npm run dev", "npm run start", "npm run preview",
        "vite", "vite build --mode production", "npm start",
        "uvicorn main:app --port 8158", "pkill -f uvicorn",
        "cd frontend && npm run build", "sleep 1; vite",
    ]:
        assert aw.is_forbidden_command(c), c
    for c in [
        "./scripts/project_build.sh", "bash script/linux/start.sh", "npm run test:unit",
        "npm install", "echo vite", "grep -r uvicorn .", "node scripts/dev.js",
    ]:
        assert not aw.is_forbidden_command(c), c
    assert len(aw.hash_command("npm run build")) == 12
    assert aw.hash_command("npm run build") != aw.hash_command("npm run dev")


def test_d7_terminal_note_dedup_and_claim_bad(tmp_path, monkeypatch):
    proj = _mk_active(tmp_path)
    monkeypatch.setattr(
        aw, "get_config",
        lambda: type("C", (), {"vibeweaver_loop_guard_enabled": True, "vibeweaver_c8_enabled": True})(),
    )
    ctx = hs.ToolHookContext(
        tool_name="terminal",
        args={"command": "npm run build"},
        conversation_id="c-c8",
        workspace_path=str(proj),
    )
    d1 = asyncio.run(aw._audit_tool_after(ctx))
    assert d1 is not None and any("vibeweaver-c8" in n for n in d1.notes)
    # 同 root 同命令去重 → 不再 note
    d2 = asyncio.run(aw._audit_tool_after(ctx))
    assert d2 is None or not any("vibeweaver-c8" in n for n in d2.notes)
    # 终局 claim：未入账 → 首次 BAD（C8 面）；入账后二次声明干净（自清可恢复）
    gate = (
        "[Verification Gate] Verifier: direct-read | Lane: S | Class: CODE | "
        "HARD-GATE-1: NO-TEST-NO-DONE=pass | HARD-GATE-2: SCRIPT-ONLY=pass\n"
        "| # | Problem | Research Sources (exa MCP / Context7) | Chosen Approach & Why "
        "| Files Changed | What Changed | Verification Evidence (Screenshot / Log) | Commit |\n"
    )
    bad = aw.audit_final_output(gate, proj)
    assert bad["verdict"] == "bad" and any("C8" in r for r in bad["reasons"]), bad
    st = vst.load_state(proj)
    assert len(st["c8Flagged"]) == 1 and st["c8Fresh"] == []
    (proj / "tests" / "verification_log.md").write_text(
        "## Block A\n- iter 1 PASS: c1\n- final-run: --final — pass=5/fail=0\n"
        "- assert-run: assert_artifacts.py: pass=5/fail=0\n", encoding="utf-8"
    )
    ok = aw.audit_final_output(gate, proj)
    assert ok["verdict"] == "ok", ok
    # 新命令（不同哈希）→ 再次 fresh
    ctx2 = hs.ToolHookContext(
        tool_name="terminal", args={"command": "pkill -f uvicorn"},
        conversation_id="c-c8", workspace_path=str(proj),
    )
    d3 = asyncio.run(aw._audit_tool_after(ctx2))
    assert d3 is not None and any("vibeweaver-c8" in n for n in d3.notes)
    bad2 = aw.audit_final_output(gate, proj)
    assert bad2["verdict"] == "bad"


# ================= D9：mode 归口 =================

def _cfg_file(tmp_path: Path, body: str):
    p = tmp_path / "config.toml"
    p.write_text("[server]\nhost='127.0.0.1'\n\n" + body, encoding="utf-8")
    return Config(str(p))


def test_d9_mode_derivation(tmp_path):
    c = _cfg_file(tmp_path, "[vibeweaver]\nmode='off'\n")
    assert c.vibeweaver_mode == "off" and not c.vibeweaver_enabled
    assert not c.vibeweaver_task_gating_enabled
    c = _cfg_file(tmp_path, "[vibeweaver]\nmode='auto'\n")
    assert c.vibeweaver_mode == "auto" and c.vibeweaver_enabled and c.vibeweaver_task_gating_enabled
    c = _cfg_file(tmp_path, "[vibeweaver]\nmode='always'\n")
    assert c.vibeweaver_mode == "always" and c.vibeweaver_enabled
    assert not c.vibeweaver_task_gating_enabled
    # 旧键推导（向后兼容）
    c = _cfg_file(tmp_path, "[vibeweaver]\nenabled=false\n")
    assert c.vibeweaver_mode == "off"
    c = _cfg_file(tmp_path, "[vibeweaver]\ntask_gating_enabled=false\n")
    assert c.vibeweaver_mode == "always" and c.vibeweaver_enabled
    c = _cfg_file(tmp_path, "[vibeweaver]\nenabled=true\ntask_gating_enabled=true\n")
    assert c.vibeweaver_mode == "auto"
    # 显式 mode 优先于旧键（单一 owner）
    c = _cfg_file(tmp_path, "[vibeweaver]\nmode='auto'\nenabled=false\n")
    assert c.vibeweaver_mode == "auto" and c.vibeweaver_enabled
    # c8 / stop_guard 子开关默认值
    assert c.vibeweaver_c8_enabled and c.vibeweaver_stop_guard_enabled


def test_d9_mode_off_install_residue(monkeypatch):
    hs.clear_hooks()

    class OffCfg:
        vibeweaver_enabled = False
        vibeweaver_mode = "off"
        vibeweaver_gate_enabled = False
        vibeweaver_audit_enabled = False
        vibeweaver_stop_guard_enabled = False
        vibeweaver_c8_enabled = False

    monkeypatch.setattr(gw, "get_config", lambda: OffCfg())
    monkeypatch.setattr(aw, "get_config", lambda: OffCfg())
    monkeypatch.setattr(sg, "get_config", lambda: OffCfg())
    monkeypatch.setattr(gw, "_installed", False)
    monkeypatch.setattr(aw, "_installed", False)
    assert gw.install_hooks()["gate_tool_hooks"] == 0
    assert aw.install_hooks()["audit_tool_hooks"] == 0
    assert sg.install_hooks()["stop_guard_hooks"] == 0
    assert hs._tool_hooks["before"] == [] and hs._tool_hooks["after"] == [] and hs._stop_hooks == []


# ================= 评审修复回归（A4.9 fix-round 第一轮） =================

def test_review_fix_no_run_does_not_consume_budget(monkeypatch):
    """A-C1 附带：无活跃 run 时 request_interrupt 不得消费预算（幻影干预）。"""
    _lg_on(monkeypatch)
    f = aw.observe_text_delta("conv-norun", wall(300), channel="content", msg_id="m1")
    assert f
    assert not aw.request_interrupt("conv-norun", "conv-norun", "fb", "text")
    o = aw._observations["conv-norun"]
    assert o.interventions.get("text", 0) == 0, "无 run 不得计入干预预算"


def test_review_fix_final_thinking_uses_conversation_key():
    """A-C1 根因判别：`_final_thinking` 发布必须用会话键（`_delivery_conv_id`），
    不得回退到不存在的 `state.session_id`（源码钉测——旧代码必红）。"""
    import inspect
    from app.services import agent_loop as al
    src = inspect.getsource(al.AgentLoop._final_thinking)
    assert "_delivery_conv_id" in src
    assert 'session_id=str(getattr(state, "session_id", "")' not in src


def test_review_fix_worker_scheduler_mode_off_override():
    """B-m5 判别：后台/定时接线必须消费 vibeweaver_mode（源码钉测）。"""
    import inspect
    from app.services import agent_scheduler as sch
    from app.services import agent_worker as wk
    assert "vibeweaver_mode" in inspect.getsource(wk)
    assert "vibeweaver_mode" in inspect.getsource(sch)
    assert "agent_loop.vibeweaver_active = False" in inspect.getsource(wk)
    assert "loop.vibeweaver_active = False" in inspect.getsource(sch)


def test_review_fix_tool_channel_rearms_on_new_turn(monkeypatch):
    """A-m5：工具通道随 turn 键（iter-N）重武装，纯工具 echo 循环不再永久闩锁。"""
    _lg_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("conv-tool", state)
    finding = None
    for i in range(4):
        finding = aw.observe_tool_call("conv-tool", "terminal", f"echo n{i}", msg_id="iter-1")
    assert finding is not None
    assert aw.request_interrupt("conv-tool", "conv-tool", "fb", "tools")
    # 同 turn 继续 echo → 闩锁抑制
    assert aw.observe_tool_call("conv-tool", "terminal", "echo more", msg_id="iter-1") is None
    # 新 turn（iter-2）→ 重武装
    f2 = None
    for i in range(4):
        f2 = aw.observe_tool_call("conv-tool", "terminal", f"echo t2-{i}", msg_id="iter-2")
    assert f2 is not None and f2["kind"] == "noop-bash"


def test_review_fix_audit_tool_after_stands_down_on_inactive_turn(tmp_path, monkeypatch):
    """A-I2：非激活轮的裸命令不得记录 c8Fresh（防后续激活轮 claim 伪 BAD）。"""
    proj = _mk_active(tmp_path)
    monkeypatch.setattr(
        aw, "get_config",
        lambda: type("C", (), {"vibeweaver_loop_guard_enabled": True, "vibeweaver_c8_enabled": True})(),
    )
    ctx = hs.ToolHookContext(
        tool_name="terminal", args={"command": "npm run build"},
        conversation_id="c-off", workspace_path=str(proj), vibeweaver_active=False,
    )
    d = asyncio.run(aw._audit_tool_after(ctx))
    assert d is None or not any("vibeweaver-c8" in n for n in (d.notes or []))
    assert "c8Fresh" not in vst.load_state(proj)


def test_review_fix_gate_off_skips_delivery(tmp_path, monkeypatch):
    """A-m3：gate_enabled=False 逃生口同时关闭投递面（JS 同语义）。"""
    dv.reset_state()
    proj = _mk_active(tmp_path)
    (proj / "memory").mkdir()
    (proj / "memory" / "t.md").write_text("---\ntriggers: [\"src/**\"]\n---\n# T\n", encoding="utf-8")
    (proj / "tests" / "working_note.md").write_text("n", encoding="utf-8")
    (proj / "src").mkdir()
    monkeypatch.setattr(gw, "get_config", lambda: type("C", (), {"vibeweaver_gate_enabled": False})())
    ctx = hs.ToolHookContext(
        tool_name="workspace_write", args={"file_path": str(proj / "src" / "a.py")},
        conversation_id="c-off-gate", workspace_path=str(proj),
    )
    d = asyncio.run(gw._tool_after_hook(ctx))
    assert d is None or not d.notes
    assert not (proj / ".vibeweaver" / "state.json").exists()


def test_review_fix_pending_resume_exact_conversation_key(tmp_path):
    """B-m3：pendingResume 仅精确会话键可消费，绝不跨会话串线。"""
    dv.reset_state()
    proj = _mk_active(tmp_path)
    (proj / "tests" / "working_note.md").write_text("ledger", encoding="utf-8")
    dv.prepare_compact_delivery(str(proj), "convA")
    assert dv.consume_pending_resume(proj, "convB") is None  # 无关会话不可消费
    got = dv.consume_pending_resume(proj, "convA")
    assert got and "ledger" in got
    # 无会话键 → 不标记（无 __any__ 幻影键）
    dv.mark_pending_resume(proj, "")
    st = json.loads((proj / ".vibeweaver" / "delivery-state.json").read_text(encoding="utf-8"))
    assert "__any__" not in (st.get("pendingResume") or {})


def test_review_fix_compressor_resolves_subdir_project_root(tmp_path):
    """B-I2：子目录项目布局下压缩投递须解析到会话项目根（注册表），
    而不是工作区根（非 active）。"""
    dv.reset_state()
    ws = tmp_path / "ws"
    proj = ws / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "verification_log.md").write_text("- iter 1 PASS: x\n", encoding="utf-8")
    (proj / "tests" / "working_note.md").write_text("subdir-ledger", encoding="utf-8")
    gw.remember_project_root("convSub", proj.resolve())
    c = ContextCompressor(quiet=True)
    c.delivery_root = str(ws)          # 工作区根（非 active）
    c.delivery_conv_id = "convSub"

    async def _fake_summary(middle, focus_topic=None, delivery_context=""):
        return "S"

    c._generate_summary_async = _fake_summary  # type: ignore[method-assign]
    msgs = [{"role": "user", "content": f"m {i} " + "x" * 80} for i in range(12)]
    out = asyncio.run(c.compress_async(msgs))
    assert any(m.get("_ephemeral") == "vibeweaver_compact_resume" for m in out), out
    st = vst.load_state(proj)
    assert st["memoryDeliveries"]["compactContext"] == 1
    assert dv.consume_pending_resume(proj, "convSub")


def test_review_fix_pending_resume_recency_refresh(tmp_path):
    """B-m4 判别：重标记刷新 recency（pop+insert），cap 逐出按最旧。"""
    dv.reset_state()
    proj = _mk_active(tmp_path)
    (proj / "tests" / "working_note.md").write_text("n", encoding="utf-8")
    dv.mark_pending_resume(proj, "convA")
    dv.mark_pending_resume(proj, "convB")
    dv.mark_pending_resume(proj, "convA")  # 刷新
    st = json.loads((proj / ".vibeweaver" / "delivery-state.json").read_text(encoding="utf-8"))
    keys = list((st.get("pendingResume") or {}).keys())
    assert keys[-1] == "convA", keys


def test_review_fix_compressor_delivery_inactive_turn_skipped(tmp_path):
    """评审 r1-B #1 判别：非激活轮（delivery_active=False）压缩不投递。"""
    dv.reset_state()
    proj = _mk_active(tmp_path)
    (proj / "tests" / "working_note.md").write_text("n", encoding="utf-8")
    c = ContextCompressor(quiet=True)
    c.delivery_root = str(proj)
    c.delivery_conv_id = "convX"
    c.delivery_active = False

    async def _fake_summary(middle, focus_topic=None, delivery_context=""):
        return "S"

    c._generate_summary_async = _fake_summary  # type: ignore[method-assign]
    msgs = [{"role": "user", "content": f"m {i} " + "x" * 80} for i in range(12)]
    out = asyncio.run(c.compress_async(msgs))
    assert not any(m.get("_ephemeral") == "vibeweaver_compact_resume" for m in out)
    assert not (proj / ".vibeweaver" / "delivery-state.json").exists()
