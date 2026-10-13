"""RED tests (A4.8) — 跨轮引用沿用守卫 + 审计对照表假判词闸门 + 指导语自救（2026-09-16）。

生产事故（conv f2553c58，60 箱，与 09-15 截断族不同病）：
  ① 写手沿用上一轮回答的 [N] 编号与论文细节（作者数/页数/DOI）——编号按轮次
     独立，本轮 [5]=GitHub issue、上轮 [5]=2607.12227 → 审计判「指向错误来源」
     （真判对）；细节来自上轮全文浏览、本轮仅摘要 → 「台账无对应证据」（真判对）。
  ② 审计员在 ~98k tokens 大上下文里判「本轮无引用编号对照表」——
     audit_citation_map 日志证明表已渲染、草稿编号全部合法（unknown=0）——
     假判词引导写手修本来就合法的编号，循环空转。
  ③ 「细节无据」类指导语未给自救路径（browser 重开原文 or 删除）。

契约（含 A4.9 R1/R2 修复轮语义）：
  A 历史存在 assistant 回答时（检测面不依赖 [N] 可见性——生产回放
    neutralize_historical_citations 恒剥离历史编号，依赖标记可见性的检测
    恒 False），本轮首次 web_search 结果的 formatted **尾部**注入跨轮引用
    纪律提醒（每轮一次）；全新会话 → 零注入零开销；插话消息不推移历史边界。
  B 审计判词声称「无对照表/对照表未列出编号有效性语境」∧ 台账非空 ∧
    草稿全部编号合法 → 前提可证伪：纯假判词（假判词从句剔除后无其他问题族）
    → 先过零引用 remand 再接收（accept_map_gate）；混合判词 → 假判词从句
    整体剔除（含硬族标记的从句 fail-closed 保留）+ 闸门说明（「」括号防
    选稿排名劫持）保留其余真问题（map_gate_corrected）。「指向错误来源」
    语义指控与「细节不在表内」真命题措辞不在范围；草稿确有越界编号 → 不介入。
  C needs_evidence/unverifiable 指导语在细节无据类判词（无对应/无法核实/
    无法核对/无证据支撑/缺乏证据）下追加自救路径：browser 重开原文取本轮
    证据，否则删除/弱化；「无证据表明 X」类普通措辞不追加。

Run: cd backend && python -m pytest tests/test_cross_turn_citation_guard.py -v
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import get_config
from app.services.agent_loop import (
    AgentLoop,
    AgentLoopState,
    ToolCallResult,
    _citation_map_false_premise,
    _has_prior_turn_assistant,
    _map_gate_has_other_issues,
    _strip_map_absent_clauses,
)

config = get_config()

USER_MSG = "你联网检索看看，尤其是看看arxiv上的对应论文，是不是大多数agent自进化论文都有我说的问题？"

# 生产判词原文（2026-09-16 60 箱）
PROBLEM_MAP_ABSENT_MIXED = (
    "草稿大量引用 [1]-[46] 编号，但本轮无引用编号对照表，无法核实编号有效性；"
    "且部分论文细节（如2605.30621、2606.11045）在台账中无对应证据。"
    "请基于本轮实际检索结果作答，并确认引用编号来源。"
)
PROBLEM_MAP_ABSENT_PURE = "本轮无引用编号对照表，无法核实编号有效性，请重新核对引用编号。"
PROBLEM_MISATTRIBUTION = (
    "草稿编号与对照表条目不对应（如[5]指向GitHub issue而非2607.12227），"
    "请按对照表改用正确编号。"
)


class ScriptedAuditor:
    """按序返回脚本结果；耗尽后返回 accept。"""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    async def __call__(self, messages, temperature=None, max_tokens=None, **kw):
        self.calls.append(list(messages))
        if self.results:
            return self.results.pop(0)
        return '{"ok": true, "problem": ""}'


class _DigestCfg:
    enabled = False


def make_loop(auditor_results=None):
    loop = AgentLoop.__new__(AgentLoop)
    loop.llm = None
    loop.iteration_llm = None
    loop.provider_type = "deepseek"
    loop.iteration_provider_type = "deepseek"
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
    loop.audit_llm = object()
    return loop


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


def _make_ledger():
    from app.services.citation_ledger import CitationLedger

    led = CitationLedger()
    led.register("https://arxiv.org/abs/2607.12227", title="自进化综述 A", snippet="")
    led.register("https://github.com/x/issue/5", title="GitHub issue 5", snippet="")
    return led


def _run_audit(loop, state, draft):
    old_npg = _set_audit_cfg("numeric_provenance_gate_mode", "off")
    try:
        return asyncio.run(loop._audit_response(state, draft, USER_MSG))
    finally:
        _restore_audit_cfg("numeric_provenance_gate_mode", old_npg)


def _search_result_payload(n=2):
    return json.dumps({
        "results": [
            {"url": f"https://example.com/{i}", "title": f"来源{i}", "snippet": f"摘要{i}"}
            for i in range(1, n + 1)
        ]
    }, ensure_ascii=False)


# ── A：跨轮引用纪律时机注入 ──────────────────────────────────────────────────

def test_has_prior_turn_assistant_detection():
    """A4.9 R1 Critical-1 回归锁：生产回放管线 neutralize_historical_citations
    会剥掉历史回答的全部 [N]（tool_history.py:100）——检测面必须是「历史存在
    assistant 回答」，绝不能依赖在 state.messages 里看到 [N] 标记。"""
    # 中性化后的历史（无任何 [N] 标记，但论文细节文字仍在）→ 必须判 True
    neutralized = AgentLoopState(messages=[
        {"role": "user", "content": "早先的问题"},
        {"role": "assistant", "content": "结论见综述与论文：RSEA 平均提升 70.7%，"
                                       "该综述共 77 页、27 位作者。"},
        {"role": "user", "content": USER_MSG},
    ])
    assert _has_prior_turn_assistant(neutralized) is True
    fresh = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    assert _has_prior_turn_assistant(fresh) is False
    # 最后一条真实 user 之后的 assistant（本轮草稿）不算历史
    current_only = AgentLoopState(messages=[
        {"role": "user", "content": USER_MSG},
        {"role": "assistant", "content": "本轮草稿内容 [1]"},
    ])
    assert _has_prior_turn_assistant(current_only) is False


def test_first_search_result_injects_note_once():
    state = AgentLoopState(messages=[
        {"role": "assistant", "content": "上一轮回答提到 RSEA 提升 70.7%（编号已中性化）。"},
        {"role": "user", "content": USER_MSG},
    ])
    r1 = ToolCallResult(call_id="c1", name="web_search",
                        arguments={"queries": ["q1"]}, result=_search_result_payload())
    AgentLoop._apply_citation_ledger(state, [r1])
    fmt1 = json.loads(r1.result)["formatted"]
    assert "跨轮引用纪律" in fmt1, "历史含 assistant 回答 → 首次检索结果注入提醒"
    assert "历史" in fmt1 and "browser" in fmt1
    # 提醒在命中条目之后（不破坏结果列表的可读顺序；UI 工具卡先出条目）
    assert fmt1.index("跨轮引用纪律") > fmt1.index("来源2")
    # 第二次检索不再重复注入
    r2 = ToolCallResult(call_id="c2", name="web_search",
                        arguments={"queries": ["q2"]}, result=_search_result_payload())
    AgentLoop._apply_citation_ledger(state, [r2])
    fmt2 = json.loads(r2.result)["formatted"]
    assert "跨轮引用纪律" not in fmt2, "每轮只注入一次"


def test_no_injection_when_history_clean():
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    r1 = ToolCallResult(call_id="c1", name="web_search",
                        arguments={"queries": ["q1"]}, result=_search_result_payload())
    AgentLoop._apply_citation_ledger(state, [r1])
    fmt1 = json.loads(r1.result)["formatted"]
    assert "跨轮引用纪律" not in fmt1, "历史干净 → 零注入"


# ── B：对照表假判词确定性闸门 ────────────────────────────────────────────────

def test_map_false_premise_detection():
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.citation_ledger = _make_ledger()
    draft = "综述 [1] 给出了四维框架，issue [2] 记录了复现问题。"
    fp, size = _citation_map_false_premise(PROBLEM_MAP_ABSENT_MIXED, state, draft)
    assert fp is True and size == 2
    assert _map_gate_has_other_issues(
        _strip_map_absent_clauses(PROBLEM_MAP_ABSENT_MIXED)) is True, "含「无对应证据」→ 混合判词"
    fp2, _ = _citation_map_false_premise(PROBLEM_MAP_ABSENT_PURE, state, draft)
    assert fp2 is True
    assert _map_gate_has_other_issues(
        _strip_map_absent_clauses(PROBLEM_MAP_ABSENT_PURE)) is False, "纯假判词（从句剔除后无剩余）"


def test_map_gate_not_triggered_by_unknown_ids():
    """草稿确有越界编号（[99] 不在台账）→ 审计有合法理由，闸门不介入。"""
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.citation_ledger = _make_ledger()
    fp, _ = _citation_map_false_premise(PROBLEM_MAP_ABSENT_PURE, state, "见 [1] 与 [99]。")
    assert fp is False


def test_map_gate_not_triggered_by_misattribution_claim():
    """「指向错误来源」语义指控（对照表存在、措辞不含缺失类标记）→ 不介入。"""
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.citation_ledger = _make_ledger()
    fp, _ = _citation_map_false_premise(PROBLEM_MISATTRIBUTION, state, "见 [1]。")
    assert fp is False


def test_map_gate_pure_false_premise_accepts(caplog):
    """纯假判词（编号全部合法、无其他问题族）→ 直接接收，不再重生成。"""
    loop = make_loop([json.dumps({
        "ok": False, "verdict": "needs_evidence", "unsupported_claims": [],
        "problem": PROBLEM_MAP_ABSENT_PURE,
    }, ensure_ascii=False)])
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.scout_used = True
    state.citation_ledger = _make_ledger()
    with caplog.at_level("INFO", logger="app.services.agent_loop"):
        verdict = _run_audit(loop, state, "综述 [1] 与 issue [2] 均已引用。")
    assert verdict is None, "纯假判词 → 接收"
    assert any("outcome=accept_map_gate" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]


def test_map_gate_pure_accept_runs_citation_remand(caplog):
    """A4.9 R1 Important-1：纯假判词接收路径不得绕过零引用 remand 闸——
    草稿通篇无 [N]（≥200 字符）且台账非空时，按既有契约先 remand 补标。"""
    loop = make_loop([json.dumps({
        "ok": False, "verdict": "needs_evidence", "unsupported_claims": [],
        "problem": PROBLEM_MAP_ABSENT_PURE,
    }, ensure_ascii=False)])
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.scout_used = True
    state.citation_ledger = _make_ledger()
    _no_cite_draft = "这份回答通篇没有任何引用标记，但讨论了检索得到的具体结论。" * 8
    with caplog.at_level("INFO", logger="app.services.agent_loop"):
        verdict = _run_audit(loop, state, _no_cite_draft)
    assert verdict is not None, "零引用长稿不得被假判词闸门直接接收"
    assert getattr(verdict, "source", "") == "citation", \
        f"应先走零引用 remand（source=citation），实得 {getattr(verdict, 'source', None)!r}"
    assert not any("outcome=accept_map_gate" in r.message for r in caplog.records)


def test_map_gate_false_positive_details_not_in_table():
    """A4.9 R1 Important-2：「对照表未列出各论文页数/作者数」是真命题（对照表
    本就只含 id/标题/URL）——收窄后的措辞词表不得命中，闸门不介入。"""
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.citation_ledger = _make_ledger()
    fp, _ = _citation_map_false_premise(
        "对照表未列出各论文的页数与作者数，这些细节无法核实，请删除或补充证据。",
        state, "综述 [1] 共 77 页、27 位作者。")
    assert fp is False


def test_map_gate_false_positive_details_with_id_context():
    """A4.9 R2 N-1：「对照表未列出编号对应的页数」——编号词在 24 字符内但
    抱怨对象是细节（真命题），正则收窄为编号有效性语境后不得命中。"""
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.citation_ledger = _make_ledger()
    fp, _ = _citation_map_false_premise(
        "对照表未列出编号对应的页数与作者数，这些细节无法核实。",
        state, "综述 [1] 共 77 页、27 位作者。")
    assert fp is False


def test_map_gate_single_clause_mixed_keeps_contradiction():
    """A4.9 R2 N-1：单从句混合判词（假判词信号+矛盾族同句）——剔除会整句
    丢失矛盾 → 必须 fail-closed 保留从句进混合路径，绝不纯接收。"""
    loop = make_loop([json.dumps({
        "ok": False, "verdict": "needs_evidence", "unsupported_claims": [],
        "problem": "本轮无引用编号对照表，且文中 47% 数据与台账矛盾。",
    }, ensure_ascii=False)])
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.scout_used = True
    state.citation_ledger = _make_ledger()
    verdict = _run_audit(loop, state, "该论文营收占比 47% [1]，issue [2] 也有讨论。")
    assert verdict is not None, "单从句含矛盾族 → 不得纯接收（闸门自身不变式）"
    assert "矛盾" in verdict.problem, "矛盾从句必须保留"
    assert "对照表核对闸门" in verdict.problem


def test_map_gate_glue_words_need_citation_context():
    """A4.9 R3 N-1 残余：「无法核实/无法核对」是 truncation 族真问题高频措辞
    （47% 营收数据无法核实），只有绑定编号语境（±12 字符内 编号/对照表/有效性）
    才允许作为假判词黏连词剔除——非编号语境按硬族保留，绝不纯接收。"""
    loop = make_loop([json.dumps({
        "ok": False, "verdict": "needs_evidence", "unsupported_claims": [],
        "problem": "本轮无引用编号对照表，且文中 47% 营收数据无法核实。",
    }, ensure_ascii=False)])
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.scout_used = True
    state.citation_ledger = _make_ledger()
    verdict = _run_audit(loop, state, "该论文营收占比 47% [1]，issue [2] 也有讨论。")
    assert verdict is not None, "非编号语境的「无法核实」=真问题，不得纯接收"
    assert "47%" in verdict.problem, "真问题从句必须保留"


def test_map_gate_short_mixed_clause_marker_self_context():
    """A4.9 R4：marker 自带语境词（对照表/编号）会让 ±12 窗口锚错——语境判定
    必须先在从句中抹除 marker 文本。短混合从句（审计员自然措辞）不得纯接收。"""
    for problem in (
        "无对照表，47% 数据无法核实。",
        "对照表缺失，47% 数据无法核实。",
        "本轮无引用编号对照表，47% 营收数据无法核实。",
    ):
        loop = make_loop([json.dumps({
            "ok": False, "verdict": "needs_evidence", "unsupported_claims": [],
            "problem": problem,
        }, ensure_ascii=False)])
        state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
        state.scout_used = True
        state.citation_ledger = _make_ledger()
        verdict = _run_audit(loop, state, "该论文数据占比 47% [1]，issue [2] 也有讨论。")
        assert verdict is not None, f"短混合从句不得纯接收：{problem}"
        assert "47%" in verdict.problem, f"真问题保留：{problem}"


def test_map_gate_independence_wording_blocks_pure_accept():
    """A4.9 R4 Minor：「独立」（自足族）与矛盾一样阻断纯接收——
    OTHER 与 HARD 对称入列。"""
    loop = make_loop([json.dumps({
        "ok": False, "verdict": "needs_evidence", "unsupported_claims": [],
        "problem": "本轮无引用编号对照表，且回答无法独立成篇。",
    }, ensure_ascii=False)])
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.scout_used = True
    state.citation_ledger = _make_ledger()
    verdict = _run_audit(loop, state, "见综述 [1] 与 issue [2]。")
    assert verdict is not None, "含自足族措辞 → 不得纯接收"


def test_interjection_does_not_shift_history_boundary():
    """A4.9 R2 N-2：插话消息（role=user, _interjection=True, 刻意非 synthetic）
    不得把扫描边界推移到本轮——全新会话本轮草稿在插话之前时，仍判无历史。"""
    state = AgentLoopState(messages=[
        {"role": "user", "content": USER_MSG},
        {"role": "assistant", "content": "本轮草稿 [1]"},
        {"role": "user", "content": "插一句嘴", "_interjection": True},
    ])
    assert _has_prior_turn_assistant(state) is False, \
        "插话前的本轮草稿不得被误判为历史 assistant"


def test_map_gate_mixed_real_issue_in_excluded_wording():
    """A4.9 R1 Important-2：真问题措辞落在「无法核实」上时不得被判「无其他
    问题」——剔除假判词从句后对剩余文本跑完整词表。"""
    loop = make_loop([json.dumps({
        "ok": False, "verdict": "needs_evidence", "unsupported_claims": [],
        "problem": "本轮无引用编号对照表，无法核实编号有效性；"
                   "且文中 47% 营收数据无法核实，请补充证据来源。",
    }, ensure_ascii=False)])
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.scout_used = True
    state.citation_ledger = _make_ledger()
    verdict = _run_audit(loop, state, "该论文营收占比 47% [1]，issue [2] 也有讨论。")
    assert verdict is not None, "混合判词（真问题在排除词上）不得被纯接收"
    assert "47%" in verdict.problem, "真问题从句必须保留"


def test_map_gate_mixed_strips_fix_citation_directive(caplog):
    """A4.9 R1 Minor-1：混合改写须真正剔除「修编号」指令——假判词从句（含
    其携带的「请重新核对引用编号」）整体从 problem 中移除，且闸门说明不带
    括号组（Minor-3：兜底选稿 _draft_ground_key 提取首个括号组，防劫持）。"""
    loop = make_loop([json.dumps({
        "ok": False, "verdict": "needs_evidence", "unsupported_claims": [],
        "problem": PROBLEM_MAP_ABSENT_MIXED,
    }, ensure_ascii=False)])
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.scout_used = True
    state.citation_ledger = _make_ledger()
    with caplog.at_level("INFO", logger="app.services.agent_loop"):
        verdict = _run_audit(loop, state, "综述 [1] 细节（77 页、27 位作者）与 issue [2]。")
    assert verdict is not None and verdict.verdict == "needs_evidence"
    assert "对照表核对闸门" in verdict.problem
    assert "请重新核对引用编号" not in verdict.problem, "假判词从句须整体剔除"
    assert "编号本身无需改动" in verdict.problem
    assert "（对照表共" not in verdict.problem, "闸门说明不得携带括号组（防选稿排名劫持）"
    assert "无对应证据" in verdict.problem, "真问题从句保留"
    assert any("outcome=map_gate_corrected" in r.message for r in caplog.records)


# ── C：细节无据类指导语自救路径 ─────────────────────────────────────────────

def test_needs_evidence_guidance_carries_browser_rescue():
    """「细节无据」类 needs_evidence 指导语追加 browser 自救路径。"""
    loop = make_loop([json.dumps({
        "ok": False, "verdict": "needs_evidence", "unsupported_claims": [],
        "problem": "草稿中作者数、页数等细节在台账中无对应证据，请补充检索或删除。",
    }, ensure_ascii=False)])
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.scout_used = True
    verdict = _run_audit(loop, state, "该综述共 77 页、27 位作者，详见正文。")
    assert verdict is not None
    assert "browser 打开对应原文" in verdict.guidance, "自救路径缺失"
    assert "删除" in verdict.guidance


def test_guidance_without_evidence_markers_has_no_rescue():
    """非「细节无据」类判词（如答非所问）→ 不追加自救路径。"""
    loop = make_loop([json.dumps({
        "ok": False, "verdict": "reject",
        "problem": "草稿答非所问：用户问的是论文核查，草稿却在讨论 harness 定义。",
    }, ensure_ascii=False)])
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.scout_used = True
    verdict = _run_audit(loop, state, "harness 的定义应当先从职责面讨论……")
    assert verdict is not None and verdict.verdict == "reject"
    assert "browser 打开对应原文" not in verdict.guidance


def test_guidance_plain_no_evidence_wording_no_rescue():
    """A4.9 R1 Minor-4：「无证据表明 X」类普通措辞不触发自救条款——
    触发词收紧为 无对应/无法核实/无法核对/无证据支撑/缺乏证据。"""
    loop = make_loop([json.dumps({
        "ok": False, "verdict": "needs_evidence", "unsupported_claims": [],
        "problem": "草稿声称该框架已被工业界广泛采用，但目前无证据表明这一说法成立，请补充来源。",
    }, ensure_ascii=False)])
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.scout_used = True
    verdict = _run_audit(loop, state, "该框架已被工业界广泛采用。")
    assert verdict is not None
    assert "browser 打开对应原文" not in verdict.guidance


# ── B2：对照表假判词「条目类型/编号需修正」形态（conv 038e837a，2026-09-22）────
# 生产判词原文（60 箱）：审计把「对照表条目=检索/浏览来源条目」误判为「编号错误」
# （细节出自来源正文/证据台账，对照表按设计只含 id/标题/URL）——「编号需修正」
# 前提被 verify.unknown 为空机械证伪。与「指向错误来源」语义指控的边界见
# test_map_gate_not_triggered_by_misattribution_claim（后者点名具体错位目标）。

# ── B2：条目类型假判词（conv 038e837a，2026-09-22）——A4.10 换向后的新契约 ─────
# 审计把「对照表条目=检索/浏览来源条目」误判为「编号错误」（「这些编号对应的是
# 检索结果条目，非论文具体细节……请核实并修正引用编号」）。该形态的「类目错误」
# vs「指向错误来源」区分必须语义判断 → 归 LLM（用户红线），确定性闸门**不介入**
# （保持原判；advisory 策略下 needs_evidence 静默出货，无重生成伤害面）。
# 结构修复在审计模板【引用编号对照表核对口径】的条目类型定义澄清（源头阻断）。

PROBLEM_WRONG_KIND = (
    "草稿引用了[11][17][22]等编号，但引用编号对照表中这些编号对应的是检索结果条目，"
    "非论文具体细节（期刊名、刊期、实验描述）。"
    "请核实并修正引用编号，确保每个事实声称有对应证据支撑。"
)


def test_map_gate_wrong_kind_left_to_llm():
    """条目类型假判词不进确定性闸门（语义判断归 LLM）——保持原判。"""
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.citation_ledger = _make_ledger()
    fp, _ = _citation_map_false_premise(
        PROBLEM_WRONG_KIND, state, "结论见综述 [1] 与 issue [2]。")
    assert fp is False, "条目类型/指向错误来源的区分是语义判断，闸门不得介入"


def test_audit_template_clarifies_entry_kind():
    """结构修复：审计模板已澄清「对照表条目即来源条目，细节在证据台账」。"""
    import app.services.agent_loop as m
    src = open(m.__file__, encoding="utf-8").read()
    assert "对照表条目即来源条目" in src
    assert "不构成编号错误" in src


def test_map_gate_absent_family_still_works():
    """回归：缺失族假判词（本闸门的设计范围）行为不变。"""
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.citation_ledger = _make_ledger()
    fp, size = _citation_map_false_premise(PROBLEM_MAP_ABSENT_PURE, state, "见 [1] 与 [2]。")
    assert fp is True and size == 2


def test_map_gate_mixed_keeps_renumber_clause_rest():
    """缺失族混合判词：保留句内只抹「修正引用编号」无效指令，
    不整句吞掉——「并补上 47% 数据的出处」必须存活。"""
    problem = (
        "本轮无引用编号对照表，无法核实编号有效性；"
        "请核实并修正引用编号，并补上 47% 数据的出处。"
    )
    stripped = _strip_map_absent_clauses(problem)
    assert "47% 数据的出处" in stripped
    assert "修正引用编号" not in stripped
    assert _map_gate_has_other_issues(stripped) is True


def test_map_gate_renumber_alone_not_strippable():
    """孤立的「修正引用编号」句不含缺失族措辞时不得整句剔除。"""
    problem = "请核实并修正引用编号，并补上 47% 数据的出处。"
    stripped = _strip_map_absent_clauses(problem)
    assert "47% 数据的出处" in stripped


def test_renumber_window_keeps_real_demand():
    """「请核实数据来源并修正引用编号」不得把「核实数据来源」一并吞掉。"""
    stripped = _strip_map_absent_clauses("请核实数据来源并修正引用编号。")
    assert "数据来源" in stripped
    assert "修正引用编号" not in stripped
