"""RED tests (A4.8) — 零引用守卫（citation remand），conv 174182bc（2026-09-15）。

生产证据（本机 8158 生产库 + chatllm.log）：
  Q3 ce86b02d：`audit_citation_map cited=0 unknown=0 ledger=18` → outcome=accept；
  Q4 999e71e1：cited=0 ledger=20 → accept；Q6 fa7bbdcb：cited=0 ledger=10 ×2 → accept。
  三条回复正文零 [N]，前端按设计只渲染被 [N] 引用的结果（MessageBubble.vue:868-943）
  → 引用角标与卡片全部缺失。写入模型 reasoning 明确规划了引用（"[2]/[11]/[13]…"）
  但最终稿丢失；审计员（同弱模型 qwen3.8-flash-next）未执行模板
  【有检索但零引用】条款。

修复契约（validator-enforces，prompt+validator 成对）：
  R1 审计出口确定性守卫：引用台账非空 + 草稿零有效 [N] + 草稿长度 ≥
     `[agent.citation] remand_min_chars` → verdict=needs_evidence（source=citation），
     guidance 含有效编号范围 + 「只补标、不重写」+ 未使用检索结果时的豁免出口。
  R2 一次性：state.citation_remanded 置位后同轮不再由守卫打回（不烧软拒预算）。
  R3 不误伤：含有效 [N] 不触发；围栏内 [N] 不计数（与 CitationLedger.verify 同口径）；
     台账 None/空不触发；低于长度阈值不触发。

Run: cd backend && python -m pytest tests/test_citation_remand_gate.py -v
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import get_config
from app.services.agent_loop import AgentLoop, AgentLoopState, ToolCallResult
from app.services.citation_ledger import CitationLedger

config = get_config()

USER_MSG = "我如果想让你为公众提供服务，需要做哪些备案手续？"

ACCEPT = '{"ok": true, "problem": ""}'


class ScriptedAuditor:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []
        self.kwargs = []

    async def __call__(self, messages, temperature=None, max_tokens=None, **kw):
        self.calls.append(list(messages))
        self.kwargs.append(dict(kw))
        if self.results:
            return self.results.pop(0)
        return ACCEPT


class _DigestCfg:
    enabled = False


def make_loop(auditor_results=None):
    loop = AgentLoop.__new__(AgentLoop)
    loop.llm = None
    loop.iteration_llm = None
    loop.provider_type = "qwen3.8_vllm"
    loop.iteration_provider_type = "qwen3.8_vllm"
    loop.iteration_extra_body_override = None
    loop.enable_reasoning = False
    loop.reasoning_effort = None
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
    loop.auditor = ScriptedAuditor(auditor_results)
    loop._complete_json = loop.auditor
    loop._try_fallback_provider = lambda: False
    loop.audit_llm = object()  # sentinel：断言 _audit_response 透传 llm=self.audit_llm
    return loop


def _set_citation_cfg(key, value):
    cit_cfg = config._config.setdefault("agent", {}).setdefault("citation", {})
    old = cit_cfg.get(key)
    cit_cfg[key] = value
    return old


def _restore_citation_cfg(key, old):
    cit_cfg = config._config.setdefault("agent", {}).setdefault("citation", {})
    if old is None:
        cit_cfg.pop(key, None)
    else:
        cit_cfg[key] = old


def _make_state(n_sources=18, draft_len_pad=400):
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    ledger = CitationLedger()
    ledger.register_hits([
        {"url": f"https://example.com/source-{i}", "title": f"检索来源{i}", "snippet": f"摘要{i}"}
        for i in range(1, n_sources + 1)
    ])
    state.citation_ledger = ledger
    state.tool_results = [
        ToolCallResult(call_id="c1", name="web_search", arguments={},
                       result='{"results": [{"url": "https://example.com/source-1"}]}'),
    ]
    return state


def _run_audit(loop, state, draft, min_chars=200):
    old = _set_citation_cfg("remand_min_chars", min_chars)
    try:
        return asyncio.run(loop._audit_response(state, draft, USER_MSG))
    finally:
        _restore_citation_cfg("remand_min_chars", old)


# ── R1 守卫触发 ────────────────────────────────────────────────────────────

def test_zero_citation_draft_remanded():
    """生产形态：台账 18 条、草稿 400+ 字零 [N] → needs_evidence / source=citation。"""
    loop = make_loop()
    state = _make_state()
    draft = "把罗航部署为面向境内公众的服务，需要办理的备案比纯网站多出 AI 专项一层。" * 12
    verdict = _run_audit(loop, state, draft)
    assert verdict is not None, "零引用草稿必须被打回（生产事故形态）"
    assert verdict.verdict == "needs_evidence"
    assert verdict.source == "citation"
    assert state.citation_remanded is True


def test_guidance_has_range_and_copy_edit_contract():
    """R1：指导语含有效编号范围 + 只补标不重写 + 独立性条款 + 未采用豁免出口。"""
    loop = make_loop()
    state = _make_state()
    draft = "把罗航部署为面向境内公众的服务，需要办理的备案比纯网站多出 AI 专项一层。" * 12
    verdict = _run_audit(loop, state, draft)
    assert "[1]-[18]" in verdict.guidance, verdict.guidance
    assert "只补标" in verdict.guidance, verdict.guidance
    assert "本轮检索结果未被采用" in verdict.guidance, verdict.guidance
    assert "完整独立" in verdict.guidance, verdict.guidance
    assert "只补标" in verdict.problem or "引用" in verdict.problem


# ── R3 不误伤 ─────────────────────────────────────────────────────────────

def test_valid_citation_not_remanded():
    loop = make_loop()
    state = _make_state()
    draft = ("把罗航部署为面向境内公众的服务，需要办理的备案比纯网站多出 AI 专项一层。" * 10
             + "算法备案与大模型登记为并行义务 [3]。")
    verdict = _run_audit(loop, state, draft)
    assert verdict is None, "含有效 [N] 的草稿不得打回"
    assert state.citation_remanded is False


def test_fenced_citation_not_counted():
    """围栏内 [N] 不是引用（与 CitationLedger.verify 同口径）→ 仍打回。"""
    loop = make_loop()
    state = _make_state()
    draft = ("把罗航部署为面向境内公众的服务，需要办理的备案比纯网站多出 AI 专项一层。" * 10
             + "\n```\n示例数组下标 a[3] 不是引用\n```\n")
    verdict = _run_audit(loop, state, draft)
    assert verdict is not None and verdict.source == "citation"


def test_unknown_only_citation_remanded():
    """越界编号（[99]）不算有效引用 → 打回（修编号而非保留无效标记）。"""
    loop = make_loop()
    state = _make_state()
    draft = ("把罗航部署为面向境内公众的服务，需要办理的备案比纯网站多出 AI 专项一层。" * 10
             + "参见 [99]。")
    verdict = _run_audit(loop, state, draft)
    assert verdict is not None and verdict.source == "citation"
    # A4.9 R2 Minor：未知编号稿不得被描述为「通篇无 [N]」，且指导语列出越界编号
    assert "通篇无 [N]" not in verdict.problem, verdict.problem
    assert "[99]" in verdict.guidance, verdict.guidance
    assert "不在本轮台账范围内" in verdict.guidance, verdict.guidance


def test_guidance_mentions_user_forbidden_clause():
    """A4.9 R2 Minor：out 子句须含「用户已明确要求不要标注来源」真实出口。"""
    loop = make_loop()
    state = _make_state()
    draft = "把罗航部署为面向境内公众的服务，需要办理的备案比纯网站多出 AI 专项一层。" * 12
    verdict = _run_audit(loop, state, draft)
    assert verdict is not None
    assert "用户已明确要求不要标注来源" in verdict.guidance, verdict.guidance


def test_zero_min_chars_disables_guard():
    """A4.9 R2 Minor：remand_min_chars=0 = 关闭守卫（与兄弟开关 0=off 一致）。"""
    loop = make_loop()
    state = _make_state()
    draft = "把罗航部署为面向境内公众的服务，需要办理的备案比纯网站多出 AI 专项一层。" * 12
    assert _run_audit(loop, state, draft, min_chars=0) is None
    assert state.citation_remanded is False


def test_empty_ledger_not_remanded():
    loop = make_loop()
    state = _make_state()
    state.citation_ledger = CitationLedger()  # 搜索失败/未搜索：空台账
    draft = "把罗航部署为面向境内公众的服务，需要办理的备案比纯网站多出 AI 专项一层。" * 12
    assert _run_audit(loop, state, draft) is None
    state.citation_ledger = None
    assert _run_audit(loop, state, draft) is None


def test_short_draft_below_min_chars_not_remanded():
    loop = make_loop()
    state = _make_state()
    draft = "备案分 ICP 与 AI 专项两层。" * 5  # < 200 chars
    assert len(draft) < 200
    assert _run_audit(loop, state, draft) is None


# ── R2 一次性 ─────────────────────────────────────────────────────────────

def test_remand_is_one_shot():
    loop = make_loop()
    state = _make_state()
    draft = "把罗航部署为面向境内公众的服务，需要办理的备案比纯网站多出 AI 专项一层。" * 12
    first = _run_audit(loop, state, draft)
    assert first is not None and first.source == "citation"
    # 2026-09-25（conv 1a24a04a，A4.9 R1 Important-1）：一次性放宽为每轮上限 2 次——
    # 修复稿仍零 [N] 时第二次 remand 拦截静默出货；第三次起放行（有界）。
    second = _run_audit(loop, state, draft)
    assert second is not None and second.source == "citation", (
        "修复稿仍零引用时必须再 remand 一次（上限 2）")
    third = _run_audit(loop, state, draft)
    assert third is None, "每轮 remand 上限 2 —— 第三次起不再由守卫打回（有界）"


def test_audit_llm_passed_through():
    """C 接线：_audit_response 的 JSON 调用必须带 llm=self.audit_llm。"""
    loop = make_loop()
    state = _make_state()
    _run_audit(loop, state, "一段普通草稿。")
    assert loop.auditor.kwargs, "审计调用必须发生"
    assert loop.auditor.kwargs[-1].get("llm") is loop.audit_llm


# ── A4.9 R1 Important-1：守卫必须覆盖所有 accept 类出口 ────────────────────
# （审计不可用/坏 JSON/非 dict/无 problem 文本时，弱审计器不得重新打开缺口）

def test_remand_fires_on_auditor_exception_fail_open():
    loop = make_loop()

    async def _boom(messages, temperature=None, max_tokens=None, **kw):
        raise RuntimeError("audit endpoint down")

    loop._complete_json = _boom
    state = _make_state()
    draft = "把罗航部署为面向境内公众的服务，需要办理的备案比纯网站多出 AI 专项一层。" * 12
    verdict = _run_audit(loop, state, draft)
    assert verdict is not None and verdict.source == "citation"


def test_remand_fires_on_bad_json_fail_open():
    loop = make_loop(["not-json-at-all", "still not json"])
    state = _make_state()
    draft = "把罗航部署为面向境内公众的服务，需要办理的备案比纯网站多出 AI 专项一层。" * 12
    verdict = _run_audit(loop, state, draft)
    assert verdict is not None and verdict.source == "citation"


def test_remand_fires_on_reject_without_problem_text():
    loop = make_loop(['{"ok": false}'])
    state = _make_state()
    draft = "把罗航部署为面向境内公众的服务，需要办理的备案比纯网站多出 AI 专项一层。" * 12
    verdict = _run_audit(loop, state, draft)
    assert verdict is not None and verdict.source == "citation"


def test_remand_fires_on_non_dict_json_fail_open(monkeypatch):
    """防御出口：slice 解析出非 dict（构造 json.loads 返回 list）→ 守卫仍生效。"""
    import app.services.agent_loop as _al

    real_loads = _al.json.loads

    def _fake_loads(s, *args, **kwargs):
        if isinstance(s, str) and s.strip().startswith('{"ok"'):
            return ["not", "a", "dict"]
        return real_loads(s, *args, **kwargs)

    monkeypatch.setattr(_al.json, "loads", _fake_loads)
    loop = make_loop(['{"ok": true}'])
    state = _make_state()
    draft = "把罗航部署为面向境内公众的服务，需要办理的备案比纯网站多出 AI 专项一层。" * 12
    verdict = _run_audit(loop, state, draft)
    assert verdict is not None and verdict.source == "citation"
