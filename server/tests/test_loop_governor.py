"""循环 Governor v2 P0+P1（设计 `docs/DESIGN_20261025_loop_guard_v2.md`）— A4.8 test-first。

覆盖：F7 停滞看门狗键名 / F9 观察表真 LRU / F2 干预预算双轨（回合 N + 会话 M）/
F4 execute_code 退款封顶 + 总迭代/工具调用/墙钟硬顶 / F1 grace+final-thinking
旁路 content 发布 / loop_detectors 纯函数抽取 + token-cycle 周期检测器 /
A3 升级阶梯（Hint→Truncate→Terminate）+ 三态熔断 / A6 off|shadow|enforce /
A1 watch_stream chunk 级断流 / A5 安装解耦（不随 vibeweaver 开关）。
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_config  # noqa: E402
from app.services import hook_service as hs  # noqa: E402
from app.services import vibeweaver_audit_service as aw  # noqa: E402
from app.services.tool_progress import resolve_tool_deadlines  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    hs.clear_hooks()
    aw._observations.clear()
    aw._active_runs.clear()
    yield
    hs.clear_hooks()
    aw._observations.clear()
    aw._active_runs.clear()


# ---------------- T1 · F7 停滞看门狗键名 ----------------

def test_f7_stall_override_key_is_registered_tool_name():
    """停滞覆盖表必须用注册名 execute_code（tools/code_execution.py），
    曾经的 `code_execution` 键精确查表不命中 → 300s 看门狗静默失效（审计 F7）。"""
    cfg = get_config()
    stall = cfg.agent_tool_loop_stall_timeout_overrides
    assert "code_execution" not in stall, "遗留键名 code_execution 仍在配置中"
    _, s = resolve_tool_deadlines(
        "execute_code",
        hard_default=cfg.agent_tool_loop_tool_call_timeout,
        hard_overrides=cfg.agent_tool_loop_tool_timeout_overrides,
        stall_overrides=stall,
    )
    assert s == 300.0


# ---------------- T2 · F9 观察表真 LRU ----------------

def test_f9_observations_true_lru():
    """容量满按**最近最少使用**淘汰（OrderedDict.move_to_end），
    旧实现按插入序弹出，误伤活跃会话（审计 F9）。"""
    for i in range(aw._MAX_OBSERVATIONS):
        aw._obs(f"obs-{i}")
    aw._obs("obs-0")  # 触达最旧键 → 变为最近使用
    aw._obs("obs-new")  # 越容量 → 触发淘汰
    assert "obs-0" in aw._observations, "被访问过的键被插入序淘汰误伤"
    assert "obs-1" not in aw._observations, "最久未用键应被淘汰"


def test_f9_active_runs_true_lru():
    class _S:
        pass

    for i in range(aw._MAX_OBSERVATIONS):
        aw.register_run(f"conv-{i}", _S())
    aw.register_run("conv-0", _S())  # 重注册 = 触达
    aw.register_run("conv-new", _S())
    assert "conv-0" in aw._active_runs, "活跃 run 被插入序淘汰误伤"
    assert "conv-1" not in aw._active_runs


# ---------------- T3 · F2 干预预算双轨（回合 N + 会话 M） ----------------

def _lg_on(monkeypatch):
    monkeypatch.setattr(aw, "get_config",
                        lambda: type("C", (), {"vibeweaver_loop_guard_enabled": True})())


def test_f2_dual_track_budget_turn_reset(monkeypatch):
    """同回合第 3 次 log-only；新回合（register_run）重置 N 但保留会话计数。"""
    _lg_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("conv-d1", state)
    assert aw.request_interrupt("conv-d1", "conv-d1", "a", "text")
    assert aw.request_interrupt("conv-d1", "conv-d1", "b", "text")
    assert not aw.request_interrupt("conv-d1", "conv-d1", "c", "text")  # 回合 N=2 耗尽
    # 旧 F2 缺陷：会话级永久 log-only。新回合必须恢复。
    aw.register_run("conv-d1", state)
    assert aw.request_interrupt("conv-d1", "conv-d1", "d", "text"), "新回合未重置每回合预算 N"


def test_f2_dual_track_budget_session_cap(monkeypatch):
    """每会话硬顶 M=8：跨回合累计到 8 后，新回合也 log-only。"""
    _lg_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    used = 0
    for turn in range(6):  # 每回合最多 2 次 → 第 4 回合起被会话帽截住
        aw.register_run("conv-d2", state)
        for _ in range(2):
            if aw.request_interrupt("conv-d2", "conv-d2", f"t{turn}", "text"):
                used += 1
    assert used == 8, f"会话硬顶应为 8，实际 {used}"
    aw.register_run("conv-d2", state)
    assert not aw.request_interrupt("conv-d2", "conv-d2", "x", "text")


# ---------------- T4 · F4 退款封顶 + 999→60 + 总迭代/工具调用/墙钟硬顶 ----------------

def test_f4_refund_cap_per_turn():
    from app.services import loop_governor_service as gov

    state = SimpleNamespace(loop_refunds=0)
    ok = [gov.allow_execute_code_refund(state, max_refunds=10) for _ in range(11)]
    assert ok.count(True) == 10, "同回合退款必须 ≤10"
    assert state.loop_refunds == 10


def test_f4_max_consecutive_is_60():
    cfg = get_config()
    assert cfg.agent_tool_loop_max_consecutive_iterations == 60, "config.toml 应已 999→60"


def test_f4_turn_hard_limits():
    from app.services import loop_governor_service as gov

    st = SimpleNamespace(turn_started_at=1000.0, iterations=10, total_tool_calls=5)
    assert gov.turn_hard_limits_hit(st, wall_seconds_eff=1200.0, now=1001.0, max_total_iterations=150) == ""
    assert gov.turn_hard_limits_hit(st, wall_seconds_eff=1200.0, now=2300.0, max_total_iterations=150) == "wall"
    st2 = SimpleNamespace(turn_started_at=1000.0, iterations=150, total_tool_calls=5)
    assert gov.turn_hard_limits_hit(st2, wall_seconds_eff=1200.0, now=1001.0, max_total_iterations=150) == "total_iterations"
    st3 = SimpleNamespace(turn_started_at=1000.0, iterations=10, total_tool_calls=100)
    assert gov.turn_hard_limits_hit(st3, wall_seconds_eff=1200.0, now=1001.0, max_total_iterations=150) == "tool_calls"


def test_f4_effective_wall_always_on():
    """不可关硬顶（qwen-code 先例）：0=关只关到 3600 兜底；调大无效。"""
    from app.services import loop_governor_service as gov

    assert gov.effective_turn_wall(0) == 3600.0
    assert gov.effective_turn_wall(1200) == 1200.0
    assert gov.effective_turn_wall(999999) == 3600.0


# ---------------- T5 · F1 grace/final-thinking 旁路 content 发布 ----------------

def test_f1_publish_helper_event_shape(monkeypatch):
    from app.services import agent_loop as al
    from app.services.agent_event_bus import agent_event_bus

    captured = []
    monkeypatch.setattr(agent_event_bus, "publish_soon", lambda ev: captured.append(ev))
    al._lg_publish_delta("conv-x", "hello", "content", "grace-3")
    assert len(captured) == 1
    ev = captured[0]
    assert ev.type == "message.delta" and ev.session_id == "conv-x"
    assert ev.data == {"delta": "hello", "channel": "content", "msg_id": "grace-3"}
    # fail-open：总线异常不得冒进生成主链路
    def _boom(_ev):
        raise RuntimeError("bus down")
    monkeypatch.setattr(agent_event_bus, "publish_soon", _boom)
    al._lg_publish_delta("c", "d", "content", "m")


def test_f1_grace_and_final_thinking_publish_content_source():
    import inspect

    from app.services import agent_loop as al

    assert "_lg_publish_delta" in inspect.getsource(al.AgentLoop._grace_call), (
        "_grace_call content 不发 delta → 终稿不被观察（审计 F1）"
    )
    assert "_lg_publish_delta" in inspect.getsource(al.AgentLoop._final_thinking), (
        "_final_thinking content 不发 delta（审计 F1）"
    )


# ---------------- T6 · loop_detectors 抽取 + token-cycle 检测器 ----------------

def test_detectors_reexport_identity():
    """抽取=行为零变化：audit 服务与 detectors 库是同一函数对象。"""
    from app.services import loop_detectors as ld

    assert aw.loop_finding is ld.loop_finding
    assert aw.noop_bash_finding is ld.noop_bash_finding
    assert aw.is_noop_bash_command is ld.is_noop_bash_command


def test_cycle_multiline_repeat_hits():
    """多行 token 周期（DRY/字符周期结构性盲区，vllm-mlx 实证形态）。"""
    from app.services.loop_detectors import token_cycle_finding

    block = "I will now create the file and write the full content for you carefully\n"
    assert token_cycle_finding("prologue text here\n" + block * 6) is not None


def test_cycle_mid_cycle_tail_triggers():
    """尾部截断在周期中间（最后一个 token 是流式词前缀）仍触发。"""
    from app.services.loop_detectors import token_cycle_finding

    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
             "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
             "oscar", "papa"]
    text = " ".join(words * 3 + words[:7] + ["al"])  # 第 4 副本吐了 7.5 个词
    f = token_cycle_finding(text)
    assert f and f["kind"] == "cycle"


def test_cycle_unique_list_clean():
    from app.services.loop_detectors import token_cycle_finding

    text = "\n".join(f"- item {i} description {i * i}" for i in range(60))
    assert token_cycle_finding(text) is None


def test_cycle_two_copies_and_short_span_clean():
    from app.services.loop_detectors import token_cycle_finding

    block = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    assert token_cycle_finding(block + " " + block) is None, "2 副本不得触发"
    assert token_cycle_finding(" ".join(["one two three"] * 8)) is None, "跨度 <48 token 不得触发"


# ---------------- T8 · A3 升级阶梯 + 三态熔断 + A6 三态模式 ----------------

class _GovCfg:
    vibeweaver_loop_guard_enabled = True
    agent_loop_governor_mode = "enforce"
    agent_loop_governor_turn_interventions = 2
    agent_loop_governor_session_interventions = 8


def _gov_on(monkeypatch, mode="enforce"):
    from app.services import loop_governor_service as gov

    cfg = type("C", (), {k: (mode if k == "agent_loop_governor_mode" else v)
                          for k, v in vars(_GovCfg).items() if not k.startswith("_")})()
    monkeypatch.setattr(gov, "get_config", lambda: cfg)
    _lg_on(monkeypatch)
    gov._run_ctxs.clear()
    gov._breaker._opened.clear()
    gov._breaker._half_open.clear()


def test_ladder_hint_truncate_terminate(monkeypatch):
    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("c-l", state)
    ctx = gov.register_run_ctx("c-l")
    f = {"kind": "repeat", "pattern": "x"}
    assert gov.escalate("c-l", f, "text", "fb1") == "hint"
    assert state.loop_guard_feedback == "fb1" and not ctx.cancel.is_set()
    assert gov.escalate("c-l", f, "text", "fb2") == "truncate"
    assert ctx.cancel.is_set() and not ctx.terminated
    assert gov.escalate("c-l", f, "text", "fb3") == "terminate"
    assert ctx.terminated and ctx.disclosure
    # Terminate 后熔断 OPEN：冷却期内后续命中 log-only
    assert gov.escalate("c-l", f, "text", "fb4") == "suppressed"


def test_breaker_cooldown_half_open_close(monkeypatch):
    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("c-b", state)
    gov.register_run_ctx("c-b")
    f = {"kind": "cycle", "pattern": "y"}
    t0 = 1000.0
    gov.escalate("c-b", f, "text", "a", now=t0)
    gov.escalate("c-b", f, "text", "b", now=t0)
    assert gov.escalate("c-b", f, "text", "c", now=t0) == "terminate"  # OPEN
    assert gov.escalate("c-b", f, "text", "d", now=t0 + 30) == "suppressed"
    # 60s 冷却 → HALF-OPEN：新回合试探放行（正常干预，不再 terminate）
    aw.register_run("c-b", state)
    gov.register_run_ctx("c-b")
    assert gov.escalate("c-b", f, "text", "e", now=t0 + 61) == "hint"
    # 试探回合正常收尾（unregister ctx）→ CLOSED
    gov.unregister_run_ctx("c-b")
    assert not gov._breaker.is_open(("c-b", "text"), now=t0 + 62)


def test_mode_shadow_no_action(monkeypatch):
    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch, mode="shadow")
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("c-s", state)
    ctx = gov.register_run_ctx("c-s")
    f = {"kind": "repeat", "pattern": "z"}
    for _ in range(4):
        assert gov.escalate("c-s", f, "text", "fb") == "shadow"
    assert state.loop_guard_feedback == "" and not ctx.cancel.is_set() and not ctx.terminated


def test_mode_off_and_env_kill(monkeypatch):
    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch, mode="off")
    assert gov.governor_mode() == "off"
    assert gov.escalate("c-o", {"kind": "repeat", "pattern": "z"}, "text", "fb") == "off"
    monkeypatch.setenv("WT_LOOP_GOVERNOR", "off")
    real_cfg = get_config()  # 真配置（enforce 也无视）
    monkeypatch.setattr(gov, "get_config", lambda: real_cfg)
    assert gov.governor_mode() == "off"


# ---------------- T9 · A1 watch_stream（消费侧观察 + chunk 级断流） ----------------

def test_watch_stream_cancel_midstream_partial_preserved(monkeypatch):
    import asyncio

    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch)
    ctx = gov.register_run_ctx("c-w1")

    async def gen():
        for i in range(10):
            if i == 3:
                ctx.cancel.set()  # chunk3 前一刻被授权断流
            yield {"type": "content", "data": f"chunk{i} "}

    async def run():
        return [ev async for ev in gov.watch_stream(gen(), conv_id="c-w1", msg_id="iter-1")]

    out = asyncio.run(run())
    assert len(out) == 3, "cancel 后 ≤1 chunk 内断流"
    assert ctx.interrupted, "断流必须留痕（披露/遥测面）"
    assert [e["data"] for e in out] == ["chunk0 ", "chunk1 ", "chunk2 "], "partial 不得静默清空"


def test_watch_stream_degenerate_stream_escalates(monkeypatch):
    """端到端：退化流 → hint→truncate（同生成内 warn→abort）→ 流被切断，
    消费方只收到前缀（F3 的 1800s 完整播放不再可达）。"""
    import asyncio

    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("c-w2", state)
    gov.register_run_ctx("c-w2")
    block = "alpha beta gamma delta\nepsilon zeta eta theta\n"

    async def gen():
        for _ in range(40):
            yield {"type": "content", "data": block}

    async def run():
        return [ev async for ev in gov.watch_stream(gen(), conv_id="c-w2", msg_id="iter-1")]

    out = asyncio.run(run())
    assert state.loop_guard_feedback and "loop-guard" in state.loop_guard_feedback
    ctx = gov.get_run_ctx("c-w2")
    # A4.9-C3：cancel 一次性消费——断流后置 interrupted 并复位，供后续流续跑
    assert ctx.interrupted and not ctx.cancel.is_set()
    assert len(out) < 40, "断流必须发生在流中段而非播完"


def test_watch_stream_truncate_recovers_next_stream(monkeypatch):
    """A4.9-C3 回归：cancel 一次性消费——挂起的 cancel 断掉一条流后即复位，
    之后的流必须正常直通（旧 bug：回合级残留 → 纠正重试轮首 chunk 即被掐死）。"""
    import asyncio

    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("c-w4", state)
    ctx = gov.register_run_ctx("c-w4")
    ctx.cancel.set()

    async def gen():
        for i in range(5):
            yield {"type": "content", "data": f"ok{i}"}

    async def run():
        return [ev async for ev in gov.watch_stream(gen(), conv_id="c-w4", msg_id="iter-2")]

    out1 = asyncio.run(run())
    assert out1 == [], "挂起的 cancel 消费掉当前这条流"
    assert ctx.interrupted and not ctx.cancel.is_set()
    ctx.interrupted = False
    out2 = asyncio.run(run())
    assert len(out2) == 5 and not ctx.interrupted, "消费后不得再掐后续流（空答回归）"


def test_watch_stream_off_mode_passthrough(monkeypatch):
    import asyncio

    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch, mode="off")
    ctx = gov.register_run_ctx("c-w3")
    ctx.cancel.set()  # off 模式：即便有 cancel 也不观察不断流（直通）

    async def gen():
        for i in range(5):
            yield {"type": "content", "data": f"x{i}"}

    async def run():
        return [ev async for ev in gov.watch_stream(gen(), conv_id="c-w3", msg_id="iter-1")]

    out = asyncio.run(run())
    assert len(out) == 5 and not ctx.interrupted


# ---------------- T10 · A4 墙钟接线（行为回归） ----------------

def test_c2_terminate_falls_through_to_grace_once(monkeypatch):
    """A4.9-C2 行为回归：硬顶命中 → 披露**一次** → grace 收口 → 生成器结束。

    旧 bug：terminate 分支 `continue` 回到同一纯函数判定 → 无限自旋刷披露
    事件、grace 永不达。源码钉测看不见控制流，此测试真跑 `_run_loop` 边界。"""
    import asyncio

    from app.services.agent_loop import AgentLoop, IterationBudget
    from app.services.agent_loop import AgentLoopState

    loop = AgentLoop(llm=SimpleNamespace(), tool_schemas=[], workspace_path="/tmp/ws")
    grace_calls = {"n": 0}

    async def fake_grace(state):
        grace_calls["n"] += 1
        yield {"content": "基于已有结果的部分回答"}

    monkeypatch.setattr(loop, "_grace_call", fake_grace)
    monkeypatch.setattr(loop, "_maybe_checkpoint", lambda state: _noop_coro())

    state = AgentLoopState()
    state.budget = IterationBudget(50)
    state.iterations = 150  # ≥ max_total_iterations(150) → total_iterations 命中
    state.messages = []

    async def run():
        events = []
        async for ev in loop._run_loop(
            state, lambda: None, SimpleNamespace(id="u1"),
            SimpleNamespace(id="conv-c2"), SimpleNamespace(id="a1"),
        ):
            events.append(ev)
            if len(events) > 200:  # 自旋哨兵
                break
        return events

    events = asyncio.run(run())
    terminates = [e for e in events if isinstance(e, dict) and (e.get("agent_step") or {}).get("name") == "loop_governor_terminate"]
    assert len(terminates) == 1, f"披露必须恰好一次，实际 {len(terminates)}（自旋回归）"
    assert grace_calls["n"] == 1, "terminate 必须落入 grace 收口一次"
    assert events[-1].get("content") == "基于已有结果的部分回答"


async def _noop_coro():
    return None


# ---------------- T11 · A5 安装解耦 ----------------

def test_a5_install_decoupled(monkeypatch):
    from app.services import loop_governor_service as gov

    off_cfg = type("C", (), {
        "vibeweaver_enabled": False, "vibeweaver_audit_enabled": False,
        "vibeweaver_loop_guard_enabled": True,
    })()
    monkeypatch.setattr(aw, "get_config", lambda: off_cfg)
    monkeypatch.setattr(gov, "get_config", lambda: off_cfg)
    monkeypatch.setattr(gov, "_gov_installed", False)
    hs.clear_hooks()
    # vibeweaver off：audit 装 0 钩子
    assert aw.install_hooks()["audit_tool_hooks"] == 0
    # governor 常驻：仍装工具观察钩子
    s = gov.install()
    assert s["governor_tool_hooks"] == 1
    assert any(n == "loop_governor" for n, _f in hs._tool_hooks["after"])
    # 幂等
    assert gov.install()["governor_tool_hooks"] == 0


def test_a5_main_py_installs_governor_outside_vibeweaver_gate():
    import inspect

    import main as main_mod

    src = inspect.getsource(main_mod)
    tail = src.split("vibeweaver hook installation failed")[1][:600]
    assert "loop_governor_service" in tail and "install" in tail, (
        "governor install 必须在 vibeweaver 门控 try 块之外"
    )


# ---------------- A4.9 fix-round 回归 ----------------

def test_i1_cycle_wired_into_observation_entry(monkeypatch):
    """A4.9-I1：token-cycle 必须挂在观察入口（此前只有单测直调=生产零武装）。

    构造**只有** cycle 能抓的形态：token 序列严格周期、但行间空白差异破坏
    行组规则 A、newline 密集使字符规则 B 被 gate 挡下。"""
    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("c-cycle1", state)
    gov.register_run_ctx("c-cycle1")
    finding = None
    for i in range(12):
        sep = "  " if i % 2 else " "  # 行间空白差异 → 行不完全相等
        block = f"I will now create the file{sep}and verify the result later{sep}"
        finding = aw.observe_text_delta("c-cycle1", block, "content", "iter-1")
        if finding:
            break
    assert finding is not None, "cycle 检测未接入观察入口"
    assert finding["kind"] == "cycle"


def test_i3_breaker_closes_on_clean_probe_turn(monkeypatch):
    """A4.9-I3：HALF-OPEN 试探回合**干净收尾**（ladder 空）也必须闭合熔断。"""
    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("c-brk", state)
    ctx = gov.register_run_ctx("c-brk")
    f = {"kind": "repeat", "pattern": "x"}
    gov.escalate("c-brk", f, "text", "a", now=1000.0)
    gov.escalate("c-brk", f, "text", "b", now=1000.0)
    assert gov.escalate("c-brk", f, "text", "c", now=1000.0) == "terminate"
    aw.register_run("c-brk", state)  # 新回合（预算重置）
    gov.unregister_run_ctx("c-brk")  # 旧 ctx 释放
    gov.register_run_ctx("c-brk")
    assert gov.escalate("c-brk", f, "text", "d", now=1061.0) != "suppressed"  # → HALF-OPEN
    gov.unregister_run_ctx("c-brk")  # 试探回合干净收尾（ladder 空）
    assert ("c-brk", "text") not in gov._breaker._opened, "试探成功必须 CLOSED（不得残留）"


def test_i4_unregister_seq_guard(monkeypatch):
    """A4.9-I4：旧回合 finally 不得弹掉新回合 ctx（重连/重试竞态）。"""
    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch)
    old_ctx = gov.register_run_ctx("c-race")
    new_ctx = gov.register_run_ctx("c-race")  # 新回合重注册
    gov.unregister_run_ctx("c-race", seq=old_ctx.seq)  # 旧 finally 迟到
    assert gov.get_run_ctx("c-race") is new_ctx, "旧代际释放误弹新回合 ctx"
    gov.unregister_run_ctx("c-race", seq=new_ctx.seq)  # 本回合收尾
    assert gov.get_run_ctx("c-race") is None


def test_i2_tool_hook_off_no_observation(monkeypatch):
    """A4.9-I2（评审 A）：mode=off 时工具钩子**不观察**（判据 #110）。"""
    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch, mode="off")
    seen = []
    real = aw.observe_tool_call
    monkeypatch.setattr(aw, "observe_tool_call",
                        lambda *a, **k: seen.append(a) or real(*a, **k))
    ctx = SimpleNamespace(tool_name="terminal", args={"command": "echo x"},
                          conversation_id="c-off1", session_id="", turn_key="iter-1")
    asyncio.run(gov._gov_tool_after(ctx))
    assert seen == [], "off 模式钩子仍在观察"


def test_cycle_copy_boundary_isolated():
    """B-M4：副本门与跨度门解耦——**≥3 连续完整副本**是硬条件（vllm-mlx 语义）：
    24 token 块 ×2+部分前缀（跨度 ≥48）不得触发；×3+部分前缀必须触发。"""
    from app.services.loop_detectors import token_cycle_finding

    block = "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty twentyone twentytwo twentythree"
    two = block + " " + block + " " + " ".join(block.split()[:5])
    assert token_cycle_finding(two) is None, "2 完整副本+部分前缀不得触发（≥3 完整副本硬条件）"
    three = " ".join([block] * 3) + " " + " ".join(block.split()[:5])
    f = token_cycle_finding(three)
    assert f and f["kind"] == "cycle", "3 完整副本+部分前缀应触发（周期中部可触发）"


# ================= 缺口修复波 G1-G6（判据 #115-#120） =================

def test_g1_tail_blocks_on_terminated(monkeypatch):
    """G1：ladder-Terminate 后截断 partial 不得静默收尾——tail 必须 blocked。"""
    from app.services import loop_governor_service as gov
    from app.services.agent_loop import AgentLoop

    _gov_on(monkeypatch)
    loop = AgentLoop(llm=SimpleNamespace(), tool_schemas=[], workspace_path="/tmp/ws")
    state = AgentLoopState_()
    ctx = gov.register_run_ctx("conv-g1")
    ctx.terminated = True
    events, blocked = asyncio.run(loop._completion_guardrails_tail(
        state, SimpleNamespace(id="conv-g1"), SimpleNamespace(id="u1"),
        "被截断的退化 partial", "",
    ))
    assert blocked is True, "terminated 置位时收尾必须被拦（截断稿不得外发）"
    assert state.turn_content_segments == []
    # R2-#124：tail 拦截**附披露事件**（#115 契约），且恰一次
    names = [(e.get("agent_step") or {}).get("name") for e in events]
    assert "loop_governor_terminate" in names, "tail 拦截必须附披露事件"
    assert gov.consume_terminate_disclosure("conv-g1") is None, "披露必须恰一次（已发过）"


def test_g2_terminate_honored_in_deathmatch(monkeypatch):
    """G2 + R2-#122：ctx.terminated 的收口对死磕生效且**走 verdict/park 出口**
    （status=paused + 落档 + 披露恰一次，不裸 return 绕 judge/落档）；
    硬顶（墙钟/迭代/工具调用）仍豁免死磕。"""
    import asyncio as _a

    from app.services import loop_governor_service as gov
    from app.services.agent_loop import AgentLoop, IterationBudget, AgentLoopState

    loop = AgentLoop(llm=SimpleNamespace(), tool_schemas=[], workspace_path="/tmp/ws")
    calls = {"judge": 0, "archive": 0, "grace": 0}

    async def _spy_judge(*a, **k):
        calls["judge"] += 1
        return {"verdict": "done", "should_continue": False}

    async def _spy_archive(dm, decision):
        calls["archive"] += 1

    async def fake_grace(state):
        calls["grace"] += 1
        yield {"content": "死磕部分回答"}

    loop._safe_judge = _spy_judge
    _patch_record_seam(monkeypatch, loop, calls)  # R3-2：spy 打在 record_harness_run
    loop._grace_call = fake_grace

    async def _noop(state):
        return None
    loop._maybe_checkpoint = _noop
    conv = SimpleNamespace(id="conv-g2dm", deathmatch_status="active", deathmatch_reason="", deathmatch_turns=0)
    loop.deathmatch_manager = SimpleNamespace(
        is_goal_active=True, _conv=conv, _freeze_wall_time=lambda: None,
        get_verdict_dict=lambda: {"status": "paused"},
    )
    state = AgentLoopState()
    state.budget = IterationBudget(50)
    state.messages = []
    state.iterations = 0  # 不触硬顶，只触 terminated
    # R2-#126：真实 register_turn（幂等）+ 置 terminated——_run_loop 重入不得
    # 换代/丢信号（A4.9-I2 自噬回归面）。
    gov._run_ctxs.clear()
    gov.register_turn(state, "conv-g2dm")
    ctx = gov.get_run_ctx("conv-g2dm")
    ctx.terminated = True

    async def run():
        evs = []
        agen = loop._run_loop(
            state, lambda: None, SimpleNamespace(id="u1"),
            SimpleNamespace(id="conv-g2dm"), SimpleNamespace(id="a1"),
        )
        async for ev in agen:
            evs.append(ev)
            if len(evs) > 200:
                break
        return evs

    evs = _a.run(run())
    assert gov.get_run_ctx("conv-g2dm") is ctx, "同回合重入不得换代 ctx（register_turn 幂等）"
    verdicts = [e for e in evs if isinstance(e, dict) and "deathmatch_verdict" in e]
    assert len(verdicts) == 1, f"verdict/park 出口必须恰一次，实际 {len(verdicts)}（自旋=未消费）"
    assert verdicts[0]["deathmatch_verdict"]["status"] == "paused"
    assert calls["archive"] == 1, (
        "record_harness_run 层恰一次落档（archive=True 兑现 #122；无标志的 paused "
        "不落=inactivity-park 语义，test_r3 双向钉）"
    )
    assert calls["judge"] == 0, "截断稿不得作 judge 依据"
    assert calls["grace"] == 0, "死磕收口走 verdict 出口，非 grace（C-1）"
    assert conv.deathmatch_status == "paused", "status 必须留终态（6b0faf81 家族回归）"
    assert gov.consume_terminate_disclosure("conv-g2dm") is None, "披露恰一次（verdict 消息携带）"


def test_g3_shadow_latches_episode(monkeypatch):
    """G3：shadow 也须 episode 闩锁——同 episode 不重复产 finding（防抖）。"""
    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch, mode="shadow")
    aw.register_run("c-sh3", SimpleNamespace(loop_guard_feedback=""))
    block = "alpha beta gamma delta\nepsilon zeta eta theta\n"
    f1 = None
    for _ in range(30):
        f1 = aw.observe_text_delta("c-sh3", block, "content", "iter-1")
        if f1:
            break
    assert f1 is not None
    assert gov.escalate("c-sh3", f1, "text", "fb") == "shadow"
    assert aw._obs("c-sh3").fired_msg_ids.get("text"), "shadow 也须记 fired_msg_ids（R2-#127 对称）"
    f2 = None
    for _ in range(30):
        f2 = aw.observe_text_delta("c-sh3", block, "content", "iter-1")
    assert f2 is None, "shadow 命中后同 episode 应闩锁（评审 B-③ 噪音）"


def test_g4_cancel_expires_at_stream_end(monkeypatch):
    """G4：流自然结束时未消费的 cancel 随生成代失效（不留残掐下一条流）。"""
    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch)
    ctx = gov.register_run_ctx("c-g4")

    async def gen():
        yield {"type": "content", "data": "x0 "}
        ctx.cancel.set()  # 置位发生在最后一个 chunk 之后——本代已无流可断

    async def run1():
        return [ev async for ev in gov.watch_stream(gen(), conv_id="c-g4", msg_id="iter-1")]

    out1 = asyncio.run(run1())
    assert len(out1) == 1
    assert not ctx.cancel.is_set(), "流自然结束须清除未消费 cancel"

    async def gen2():
        for i in range(3):
            yield {"type": "content", "data": f"y{i} "}

    async def run2():
        return [ev async for ev in gov.watch_stream(gen2(), conv_id="c-g4", msg_id="iter-2")]

    out2 = asyncio.run(run2())
    assert len(out2) == 3 and not ctx.interrupted, "残留不得掐死下一条流"


def test_g5_bad_config_values_fall_back(tmp_path):
    """G5：坏配置值不得让 governor property 抛异常（回退默认）。"""
    from app.core.config import Config

    p = tmp_path / "config.toml"
    p.write_text(
        "[server]\nhost='127.0.0.1'\n\n"
        "[agent.loop_governor]\n"
        'turn_interventions="abc"\n'
        'session_interventions=""\n'
        'turn_wall_time_seconds="xyz"\n'
        'refund_max_per_turn="!"\n'
        'max_total_iterations="n/a"\n'
        'cycle_period_max="o"\n'
        'cycle_min_copies="p"\n'
        'polling_tools="notalist"\n'
        "exempt_tools=42\n",
        encoding="utf-8",
    )
    c = Config(str(p))
    assert c.agent_loop_governor_turn_interventions == 2
    assert c.agent_loop_governor_session_interventions == 8
    assert c.agent_loop_governor_turn_wall_time_seconds == 0.0
    assert c.agent_loop_governor_refund_max_per_turn == 10
    assert c.agent_loop_governor_max_total_iterations == 150
    assert c.agent_loop_governor_cycle_period_max == 128
    assert c.agent_loop_governor_cycle_min_copies == 3
    assert c.agent_loop_governor_polling_tools == []
    assert c.agent_loop_governor_exempt_tools == []


def test_g6_new_turn_rearms_latches(monkeypatch):
    """G6：episode 键 iter-N 跨回合复用——新回合必须清 armed/turn_chars，
    否则上回合 fired 闩锁把新回合首条退化流整回合静音。"""
    _lg_on(monkeypatch)
    state = SimpleNamespace(loop_guard_feedback="")
    aw.register_run("conv-g6", state)
    block = "alpha beta gamma delta\nepsilon zeta eta theta\n"
    f1 = None
    for _ in range(30):
        f1 = aw.observe_text_delta("conv-g6", block, "content", "iter-1")
        if f1:
            break
    assert f1 is not None
    assert aw.request_interrupt("conv-g6", "conv-g6", "fb", "text")  # fired → armed=False
    f2 = None
    for _ in range(30):
        f2 = aw.observe_text_delta("conv-g6", block, "content", "iter-1")
    assert f2 is None, "同回合闩锁（既有语义）应保持"
    # 新回合
    aw.register_run("conv-g6", state)
    obs = aw._observations["conv-g6"]
    assert obs.turn_chars == 0, "新回合 turn_chars 应重置（episode 键复用泄漏）"
    assert obs.fired_msg_ids == {}, "R3-3：新回合必须清 fired 闩锁（注释兑现+钉测）"
    f3 = None
    for _ in range(30):
        f3 = aw.observe_text_delta("conv-g6", block, "content", "iter-1")
        if f3:
            break
    assert f3 is not None, "新回合必须重武装（G6 泄漏回归）"


def AgentLoopState_():
    """G1 用最小 state（SimpleNamespace 面字段）。"""
    st = SimpleNamespace(
        loop_guard_feedback="", messages=[], turn_content_segments=[],
        session_id="s", stop_hook_blocks=0,
    )
    return st


# ================= fix-round 2（判据 #122-#127） =================

async def _drain(agen):
    return [ev async for ev in agen]


def test_r2_deathmatch_finalize_skips_judge_and_verdicts(monkeypatch):
    """#122/#123（A4.9-C1/B-1）：死磕 finalize 入口拦截——截断稿不得作 judge
    依据；收口走 verdict/park 出口（落档 + status 终态 + 披露恰一次）。"""
    import asyncio as _a

    from app.services import loop_governor_service as gov
    from app.services.agent_loop import AgentLoop, AgentLoopState
    from app.services.agent_loop_orchestrator import _LoopReturnSignal

    loop = AgentLoop(llm=SimpleNamespace(), tool_schemas=[], workspace_path="/tmp/ws")
    calls = {"judge": 0, "archive": 0}

    async def _spy_judge(*a, **k):
        calls["judge"] += 1
        return {"verdict": "done", "should_continue": False}

    async def _spy_archive(dm, decision):
        calls["archive"] += 1

    loop._safe_judge = _spy_judge
    _patch_record_seam(monkeypatch, loop, calls)  # R3-2：spy 打在 record_harness_run
    conv = SimpleNamespace(id="conv-r2a", deathmatch_status="active", deathmatch_reason="", deathmatch_turns=0)
    loop.deathmatch_manager = SimpleNamespace(
        is_goal_active=True, is_active=True, _conv=conv, _freeze_wall_time=lambda: None,
        get_verdict_dict=lambda: {"status": "paused"},
    )
    loop._delivery_conv_id = "conv-r2a"
    gov._run_ctxs.clear()
    ctx = gov.register_run_ctx("conv-r2a")
    ctx.terminated = True

    async def run():
        evs = []
        with pytest.raises(_LoopReturnSignal):
            async for ev in loop._finalize_deathmatch_branch(AgentLoopState(), None):
                evs.append(ev)
        return evs

    evs = _a.run(run())
    assert calls["judge"] == 0, "截断稿不得进 judge"
    assert calls["archive"] == 1, (
        "record_harness_run 层恰一次落档（archive=True 兑现 #122）"
    )
    verdicts = [e for e in evs if "deathmatch_verdict" in e]
    assert len(verdicts) == 1 and verdicts[0]["deathmatch_verdict"]["status"] == "paused"
    assert conv.deathmatch_status == "paused"
    assert gov.consume_terminate_disclosure("conv-r2a") is None, "披露恰一次（verdict 消息携带）"


def test_r2_empty_branch_converges_on_terminated():
    """#123（A4.9-B-2）：空 partial + terminated 不得静默 done/retry。"""
    import asyncio as _a

    from app.services import loop_governor_service as gov
    from app.services.agent_loop import AgentLoop, AgentLoopState
    from app.services.agent_loop_orchestrator import _LoopContinueSignal

    loop = AgentLoop(llm=SimpleNamespace(), tool_schemas=[], workspace_path="/tmp/ws")
    loop.deathmatch_manager = None
    loop._delivery_conv_id = "conv-r2b"
    gov._run_ctxs.clear()
    ctx = gov.register_run_ctx("conv-r2b")
    ctx.terminated = True
    state = AgentLoopState()
    state.empty_answer_retries = 99  # retry 耗尽形态
    with pytest.raises(_LoopContinueSignal):
        _a.run(_drain(loop._finalize_empty_answer_branch(state, "")))
    # 无 terminated：retry 耗尽 → done-empty（既有行为不变）
    gov.unregister_run_ctx("conv-r2b")
    state2 = AgentLoopState()
    state2.empty_answer_retries = 99
    evs = _a.run(_drain(loop._finalize_empty_answer_branch(state2, "")))
    assert evs and evs[-1].get("empty") is True


def test_r2_g4_generation_guard(monkeypatch):
    """#125（A4.9-I1）：旧 wrapper 迟到收尾不得清掉**新代** cancel。"""
    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch)
    gov._run_ctxs.clear()
    old_ctx = gov.register_run_ctx("c-r2c")

    async def gen():
        yield {"type": "content", "data": "x "}

    async def run():
        out = []
        agen = gov.watch_stream(gen(), conv_id="c-r2c", msg_id="iter-1")
        async for ev in agen:
            out.append(ev)
            break  # 生产消费方超时/截断的形态——流悬挂未关
        new_ctx = gov.register_run_ctx("c-r2c")  # 回合换代
        new_ctx.cancel.set()  # 新代合法断流信号
        await agen.aclose()  # 旧 wrapper 迟到收尾（GC/asyncgen finalizer 时点）
        return out, new_ctx

    out, new_ctx = asyncio.run(run())
    assert out, "旧代至少透传首 chunk"
    assert new_ctx.cancel.is_set(), "旧 wrapper 收尾不得清掉新代 cancel（跨代信号丢失）"


def test_r2_register_turn_idempotent(monkeypatch):
    """#126（A4.9-I2 自噬修复）：同回合二次注册不换代、不丢 terminated、不翻台账。"""
    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch)
    gov._run_ctxs.clear()
    aw._observations.clear()
    state = SimpleNamespace(loop_guard_feedback="", gov_ctx_seq=0, turn_started_at=0.0)
    gov.register_turn(state, "c-r2d")
    ctx = gov.get_run_ctx("c-r2d")
    obs = aw._obs("c-r2d")
    obs.turn_interventions = {"text": 1}
    ctx.terminated = True
    seq1 = state.gov_ctx_seq
    gov.register_turn(state, "c-r2d")  # _run_loop 重入
    assert gov.get_run_ctx("c-r2d") is ctx, "同回合不得换代"
    assert ctx.terminated, "二次注册不得丢 terminated"
    assert obs.turn_interventions == {"text": 1}, "二次注册不得重置台账"
    assert state.gov_ctx_seq == seq1


def test_r2_direct_reply_observation_composition(monkeypatch):
    """#126：直答观察组合（register_turn + watch_stream 发现退化并断流）。
    wrap 调用点本身由 FCV 读码核对（判据措辞允许）。"""
    from app.services import loop_governor_service as gov

    _gov_on(monkeypatch)
    aw._observations.clear()
    gov._run_ctxs.clear()
    state = SimpleNamespace(loop_guard_feedback="", gov_ctx_seq=0, turn_started_at=0.0)
    gov.register_turn(state, "c-r2e")
    block = "alpha beta gamma delta\nepsilon zeta eta theta\n"

    async def dr_gen():
        for _ in range(40):
            yield {"type": "content", "data": block}

    async def run():
        return [ev async for ev in gov.watch_stream(dr_gen(), conv_id="c-r2e", msg_id="direct-reply")]

    out = asyncio.run(run())
    assert state.loop_guard_feedback and "loop-guard" in state.loop_guard_feedback
    assert len(out) < 40, "退化直答必须被断（观察组合生效）"


# ================= fix-round 3（判据 #122 落档 + R3 Critical 回归钉） =================

def _patch_record_seam(monkeypatch, loop, calls):
    """R3-2 非自证 seam：spy 打在 `record_harness_run`（落档真入口），
    `_maybe_archive_deathmatch` 保持真实闸门——archive 标志不落即 RED。"""
    import app.services.agent_loop as _al

    async def _spy_record(db, conv, decision):
        calls["archive"] += 1

    monkeypatch.setattr(_al, "record_harness_run", _spy_record)

    class _FakeDB:
        async def execute(self, *a, **k):
            return None

        async def commit(self):
            return None

    class _FakeSession:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, *a):
            return False

    loop.session_factory = lambda: _FakeSession()


def test_r3_nondeathmatch_finalize_not_intercepted():
    """R3-1（复审 Critical 回归钉）：非死磕 terminated 内容回合不得被
    `_finalize_deathmatch_branch` 入口拦截吞掉——必须放行到 tail
    （G1 打回 + 披露恰一次），复刻裸 return 族即红。"""
    import asyncio as _a

    from app.services import loop_governor_service as gov
    from app.services.agent_loop import AgentLoop, AgentLoopState

    loop = AgentLoop(llm=SimpleNamespace(), tool_schemas=[], workspace_path="/tmp/ws")
    loop.deathmatch_manager = None
    loop._delivery_conv_id = "conv-r3a"
    gov._run_ctxs.clear()
    ctx = gov.register_run_ctx("conv-r3a")
    ctx.terminated = True

    class _LR:
        _final_content = "截断 partial"
        _dm_should_continue = None
        _ev = None
        _is_repeating = None
        _reset_ev = None
        _restart = None
        _search_only = None
        _turns = 0
        compressed = None
        decision = None
        dm = None
        rep_prompt = None
        retry_prompt = None

    async def run():
        evs = []
        async for ev in loop._finalize_deathmatch_branch(AgentLoopState(), _LR()):
            evs.append(ev)
        return evs

    evs = _a.run(run())  # 不得 raise _LoopReturnSignal（裸 return 族回归即红）
    assert evs == [], "非死磕 finalize 分支不得拦截产事件"
    # 组合证明：tail 仍拦得住（G1 打回 + 披露恰一次）
    state = AgentLoopState_()
    events, blocked = _a.run(loop._completion_guardrails_tail(
        state, SimpleNamespace(id="conv-r3a"), SimpleNamespace(id="u1"), "截断 partial", "",
    ))
    assert blocked is True
    assert any((e.get("agent_step") or {}).get("name") == "loop_governor_terminate" for e in events)


def test_r3_terminate_park_archives_harness_row(monkeypatch):
    """R3-2（#122 落档兑现）：paused park 显式 archive=True → record_harness_run
    恰一次；无 archive 标志（inactivity 同型）不落档——闸门语义不回归。"""
    import asyncio as _a

    from app.services import loop_governor_service as gov
    from app.services.agent_loop import AgentLoop

    loop = AgentLoop(llm=SimpleNamespace(), tool_schemas=[], workspace_path="/tmp/ws")
    calls = {"archive": 0}
    _patch_record_seam(monkeypatch, loop, calls)
    conv = SimpleNamespace(id="conv-r3b", deathmatch_status="active", deathmatch_reason="", deathmatch_turns=0)
    dm = SimpleNamespace(
        is_goal_active=True, _conv=conv, _freeze_wall_time=lambda: None,
        get_verdict_dict=lambda: {"status": "paused"},
    )
    loop.deathmatch_manager = dm
    loop._delivery_conv_id = "conv-r3b"
    gov._run_ctxs.clear()
    ctx = gov.register_run_ctx("conv-r3b")
    ctx.terminated = True

    evs = _a.run(_drain(loop._gov_terminate_paused(dm)))
    assert len(evs) == 1 and "deathmatch_verdict" in evs[0]
    assert calls["archive"] == 1, "paused park（archive=True）必须落档 record_harness_run"
    # 无 archive 标志（inactivity-park 同型决策）→ 不落档（既有语义保持）
    calls["archive"] = 0
    conv2 = SimpleNamespace(id="conv-r3c", deathmatch_status="active", deathmatch_reason="", deathmatch_turns=0)
    _a.run(loop._maybe_archive_deathmatch(
        SimpleNamespace(_conv=conv2), {"status": "paused", "verdict": "continue"}))
    assert calls["archive"] == 0, "无 archive 标志的 paused 不落档（inactivity-park 语义保持）"


def test_r3_manager_inactive_not_intercepted():
    """R3 复审 Minor-1（测试强度）：`is_active` 子句是 load-bearing——manager 在场
    但非 active（done/human_gate/partial_complete 回合）时同样不得拦截：
    删该子句即复刻 Critical（_gov_terminate_paused 早退 + 裸 raise 吞 G1）。"""
    import asyncio as _a

    from app.services import loop_governor_service as gov
    from app.services.agent_loop import AgentLoop, AgentLoopState

    loop = AgentLoop(llm=SimpleNamespace(), tool_schemas=[], workspace_path="/tmp/ws")
    conv = SimpleNamespace(id="conv-r3d", deathmatch_status="done", deathmatch_reason="", deathmatch_turns=0)
    loop.deathmatch_manager = SimpleNamespace(
        is_goal_active=False, is_active=False, _conv=conv,
        _freeze_wall_time=lambda: None, get_verdict_dict=lambda: {},
    )
    loop._delivery_conv_id = "conv-r3d"
    gov._run_ctxs.clear()
    ctx = gov.register_run_ctx("conv-r3d")
    ctx.terminated = True

    class _LR:
        _final_content = "截断 partial"
        _dm_should_continue = None
        _ev = None
        _is_repeating = None
        _reset_ev = None
        _restart = None
        _search_only = None
        _turns = 0
        compressed = None
        decision = None
        dm = None
        rep_prompt = None
        retry_prompt = None

    async def run():
        evs = []
        async for ev in loop._finalize_deathmatch_branch(AgentLoopState(), _LR()):
            evs.append(ev)
        return evs

    evs = _a.run(run())
    assert evs == [], "manager 非 active 时不得拦截产事件（is_active 子句 load-bearing）"
    state = AgentLoopState_()
    _events, blocked = _a.run(loop._completion_guardrails_tail(
        state, SimpleNamespace(id="conv-r3d"), SimpleNamespace(id="u1"), "截断 partial", "",
    ))
    assert blocked is True, "tail 仍须拦住（G1 路径不被吞）"
