"""RED tests (A4.8) — conv 3a216a51（2026-09-02 23:38，60 箱生产）：
NPG enforce 打回循环无收敛守卫 + salvage 全力思考静默 ~120s。

生产时间线（/root/weavethinker/chatllm.log，DB 为 UTC）：
  iter1-4 audit_metric outcome=needs_evidence npg=enforce flags=3→6→6→8，
  total_tool_calls=0（模型从未产出 calculate/execute_code 回执——法条/章程
  措辞题的数值是法定事实，不可计算自救）；soft 预算 4 连烧 → salvage；
  salvage 以 enable_reasoning=True（xhigh）全量缓冲生成 ~120s，SSE 零事件，
  用户 23:42:21 取消 SSE；23:42:36 兜底稿落库。用户观感=「循环数遍后卡死」。

修复契约：
  R1 NPG no-progress 守卫：连续两次 NPG enforce 打回，flags 不降且期间无新
     工具调用 → 视同预算耗尽，直接进入 salvage（不再整稿重发）。
  R2 salvage / best-of selection 生成阶段思考默认关闭（新配置
     [agent.audit] salvage_thinking_enabled，默认 false），与打回修正轮
     revision_thinking_enabled=false（用户 2026-08-21 决定）同口径——
     修正轮 12-13s 出稿，salvage 却 xhigh 思考 120s，口径不一致。
  R3 NPG enforce 打回在 state 上留下 (flags, tool_calls) 历史（守卫输入）。

Run: cd backend && python -m pytest tests/test_npg_no_progress_salvage.py -v
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import get_config
from app.services.agent_loop import AgentLoop, AgentLoopState, ToolCallResult
from app.services.numeric_provenance_gate import evaluate_numeric_provenance

config = get_config()

STRAWBERRY_USER_MSG = "请帮我计算穴盘锥管上口内65mm，下口内直径25mm,长度110mm,的容积"

STRAWBERRY_DRAFT = (
    "锥管为上口内径 65mm、下口内径 25mm、长度 110mm 的截锥体，按截锥体容积公式计算：\n"
    "$$V = \\frac{\\pi \\times 110}{3}(32.5^2 + 32.5 \\times 12.5 + 12.5^2) \\approx 40000\\ \\text{mm}^3 = 40\\ \\text{ml}$$\n"
    "**该锥管容积约为 40 毫升。**"
)


class ScriptedAuditor:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def __call__(self, messages, temperature=None, max_tokens=None, **kw):
        self.calls.append(list(messages))
        if self.results:
            return self.results.pop(0)
        return '{"ok": true, "problem": ""}'


class _DigestCfg:
    enabled = False


class StreamStub:
    """Captures stream_chat_structured kwargs; yields scripted events."""

    def __init__(self, events=None):
        self.events = events or [{"type": "content", "data": "兜底稿正文"}]
        self.captured = {}

    async def stream_chat_structured(self, messages, tools=None, extra_body=None, **kw):
        self.captured["extra_body"] = extra_body
        self.captured["kwargs"] = kw
        self.captured["messages"] = messages
        for e in self.events:
            yield e


def make_salvage_loop(auditor_results=None, enable_reasoning=True):
    loop = AgentLoop.__new__(AgentLoop)
    loop.llm = StreamStub()
    loop.iteration_llm = None
    loop.provider_type = "deepseek"
    loop.iteration_provider_type = "deepseek"
    loop.iteration_extra_body_override = None
    loop.enable_reasoning = enable_reasoning
    loop.reasoning_effort = "max" if enable_reasoning else None
    loop.thinking_budget = None
    loop.preserve_thinking = False
    loop.deathmatch_manager = None
    loop._compressor = None
    loop.enable_compression = False
    loop._canary_enabled = False
    loop._canary_marker = None
    loop.max_iterations = 30
    loop.tool_schemas = []
    loop._active_tool_names = set()
    loop.session_factory = None
    loop._tool_call_timeout = 60
    loop._digest_config = _DigestCfg()
    loop.workspace_path = None
    loop.auditor = ScriptedAuditor(auditor_results or [])
    loop._complete_json = loop.auditor
    loop._try_fallback_provider = lambda: False
    return loop


def make_plain_loop(auditor_results=None):
    loop = make_salvage_loop(auditor_results=auditor_results, enable_reasoning=False)
    loop.llm = None
    return loop


def _make_state():
    return AgentLoopState(messages=[{"role": "user", "content": STRAWBERRY_USER_MSG}])


def _set_audit_cfg(key, value):
    audit_cfg = config._config.setdefault("agent", {}).setdefault("audit", {})
    old = audit_cfg.get(key)
    audit_cfg[key] = value
    return old


def _restore_audit_cfg(key, old):
    audit_cfg = config._config.setdefault("agent", {}).setdefault("audit", {})
    if old is None:
        audit_cfg.pop(key, None)
    else:
        audit_cfg[key] = old


# ── R1: no-progress 守卫（纯函数，churn 感知） ─────────────────────────────

def test_r1_no_progress_raws_superset_no_tools():
    from app.services.agent_loop import _npg_soft_reject_no_progress
    # 生产事故形态：旧 flag 值一个没修，还越改越多（3→6→6→8）
    assert _npg_soft_reject_no_progress(
        [3, 6], [0, 0], [frozenset({"a", "b", "c"}), frozenset({"a", "b", "c", "d", "e", "f"})]
    ) is True


def test_r1_no_progress_raws_equal_no_tools():
    from app.services.agent_loop import _npg_soft_reject_no_progress
    assert _npg_soft_reject_no_progress(
        [6, 6], [1, 1], [frozenset({"a", "b"}), frozenset({"a", "b"})]
    ) is True


def test_r1_progress_raws_shrunk():
    from app.services.agent_loop import _npg_soft_reject_no_progress
    assert _npg_soft_reject_no_progress(
        [3, 2], [0, 0], [frozenset({"a", "b", "c"}), frozenset({"a"})]
    ) is False


def test_r1_progress_churn_fixed_old_added_new():
    from app.services.agent_loop import _npg_soft_reject_no_progress
    # A4.9 Important-2：修 5 个旧值又引入 1 个新值（raws 互不包含）≠ 停滞
    assert _npg_soft_reject_no_progress(
        [6, 6], [0, 0], [frozenset({"a", "b", "c", "d", "e", "f"}), frozenset({"g"})]
    ) is False


def test_r1_progress_new_tool_calls():
    from app.services.agent_loop import _npg_soft_reject_no_progress
    assert _npg_soft_reject_no_progress(
        [3, 6], [0, 2], [frozenset({"a"}), frozenset({"a", "b"})]
    ) is False


def test_r1_single_reject_never_triggers():
    from app.services.agent_loop import _npg_soft_reject_no_progress
    assert _npg_soft_reject_no_progress([3], [0], [frozenset({"a"})]) is False
    assert _npg_soft_reject_no_progress([], [], []) is False


# ── R3: NPG enforce 打回在 state 上留历史 ────────────────────────────────────

def test_r3_npg_enforce_reject_records_history():
    loop = make_plain_loop(auditor_results=['{"ok": true, "problem": ""}'])
    state = _make_state()
    old = _set_audit_cfg("numeric_provenance_gate_mode", "enforce")
    try:
        v = asyncio.run(loop._audit_response(state, STRAWBERRY_DRAFT, STRAWBERRY_USER_MSG))
    finally:
        _restore_audit_cfg("numeric_provenance_gate_mode", old)
    assert v is not None and v.verdict == "needs_evidence"
    expected = len(evaluate_numeric_provenance(
        draft=STRAWBERRY_DRAFT, evidence_text="", user_messages=[STRAWBERRY_USER_MSG],
        current_tool_calls=[], tolerance=0.01,
    ).flags)
    assert expected >= 1
    assert state.npg_reject_flags == [expected], state.npg_reject_flags
    assert state.npg_reject_tool_calls == [state.total_tool_calls]
    assert len(state.npg_reject_raws) == 1 and len(state.npg_reject_raws[0]) == expected
    assert v.source == "npg", v.source  # A4.9 Important-1：守卫只认 npg 来源


def test_r3b_npg_off_records_nothing():
    loop = make_plain_loop(auditor_results=['{"ok": true, "problem": ""}'])
    state = _make_state()
    old = _set_audit_cfg("numeric_provenance_gate_mode", "off")
    try:
        v = asyncio.run(loop._audit_response(state, STRAWBERRY_DRAFT, STRAWBERRY_USER_MSG))
    finally:
        _restore_audit_cfg("numeric_provenance_gate_mode", old)
    assert state.npg_reject_flags == []
    assert state.npg_reject_tool_calls == []
    assert state.npg_reject_raws == []
    assert v is None or v.source == "llm"  # LLM 审计路径不得带 npg 来源


def test_r3c_llm_auditor_needs_evidence_is_llm_source():
    """A4.9 Important-1：LLM 审计员的 needs_evidence 与 NPG 历史无关，
    source 必须是 llm（决策点据此不触发守卫）。"""
    loop = make_plain_loop(auditor_results=[
        '{"verdict": "needs_evidence", "problem": "需要补充证据", "guidance": "请核实"}'
    ])
    state = _make_state()
    state.npg_reject_flags = [3]
    state.npg_reject_tool_calls = [0]
    state.npg_reject_raws = [frozenset({"40000"})]
    old = _set_audit_cfg("numeric_provenance_gate_mode", "off")
    try:
        v = asyncio.run(loop._audit_response(state, "一段普通草稿", STRAWBERRY_USER_MSG))
    finally:
        _restore_audit_cfg("numeric_provenance_gate_mode", old)
    assert v is not None and v.verdict == "needs_evidence"
    assert v.source == "llm", v.source


# ── R2: salvage 生成阶段思考口径 ─────────────────────────────────────────────

def _run_salvage(loop, state):
    async def _collect():
        out = []
        async for ev in loop._salvage_after_audit_budget(state, STRAWBERRY_USER_MSG):
            out.append(ev)
        return out

    return asyncio.run(_collect())


def test_r2_salvage_thinking_off_by_default():
    loop = make_salvage_loop()  # enable_reasoning=True, effort=max
    state = _make_state()
    old = _set_audit_cfg("numeric_provenance_gate_mode", "off")
    try:
        events = _run_salvage(loop, state)
    finally:
        _restore_audit_cfg("numeric_provenance_gate_mode", old)
    extra = loop.llm.captured.get("extra_body") or {}
    assert extra.get("thinking", {}).get("type") == "disabled", extra
    assert "reasoning_effort" not in extra, extra
    assert any("content" in e for e in events), events  # salvage yield 契约：{"content": ...}


def test_r2b_salvage_thinking_config_reenables():
    loop = make_salvage_loop()
    state = _make_state()
    old_mode = _set_audit_cfg("numeric_provenance_gate_mode", "off")
    old_think = _set_audit_cfg("salvage_thinking_enabled", True)
    try:
        _run_salvage(loop, state)
    finally:
        _restore_audit_cfg("numeric_provenance_gate_mode", old_mode)
        _restore_audit_cfg("salvage_thinking_enabled", old_think)
    extra = loop.llm.captured.get("extra_body") or {}
    assert extra.get("thinking", {}).get("type") == "enabled", extra
    assert extra.get("reasoning_effort") == "max", extra


# ── R2 续: selection（best-of）同一口径 ─────────────────────────────────────

def test_r2c_selection_thinking_off_by_default():
    loop = make_salvage_loop()
    state = _make_state()
    # 预置一条被拒稿，让 selection 走 stash 路径
    state.audit_rejected_drafts = [{
        "content": "被拒草稿A" * 40, "verdict": "needs_evidence",
        "problem": "数值无回执", "source": "draft", "reasoning": "",
    }]
    old_mode = _set_audit_cfg("numeric_provenance_gate_mode", "off")
    try:
        async def _collect():
            out = []
            async for ev in loop._select_best_rejected_draft(state, STRAWBERRY_USER_MSG):
                out.append(ev)
            return out
        asyncio.run(_collect())
    finally:
        _restore_audit_cfg("numeric_provenance_gate_mode", old_mode)
    extra = loop.llm.captured.get("extra_body")
    assert extra is not None, "selection 应走流式生成路径（stash 非空）"  # 防空转通过
    assert extra.get("thinking", {}).get("type") == "disabled", extra
