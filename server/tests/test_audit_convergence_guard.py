"""RED tests (A4.8) — 审计循环收敛加固（conv f2553c58，2026-09-15 60 箱生产）。

生产证据（/root/weavethinker/chatllm.log 21:50:49→22:08:47 + 生产库 LIKE 统计）：
  conv f2553c58 Q2：
    audit_evidence_full chars=1111817 items=20 tokens_pre~278187 capped=False
    审计可见预算 = min(128000, 128000−12000) = 116000 tokens → 约 58% 证据不可见
    4 连 needs_evidence「草稿大量具体数值…落在截断证据范围内无法核对」
    → salvage（同判被拒）→ selection（同判被拒）→ selection_deterministic
    drafts=5 → 兜底稿带「多轮修正仍未通过校验」警示前缀落库，全程 18 分钟。
  生产库统计：该警示文案 39 条 + 旧失败文案 5 条 = 44 次（09-15 单日 16 次）。

修复契约（validator-enforces；SOTA：确定性校验优先于 LLM 自评，证据对齐是
分解验证的瓶颈——research 见 memory/fix_audit_convergence_guard_20260915.md）：
  R1 可见性接收闸门：LLM 审计的非 accept 判决（unverifiable / 可见性族
     needs_evidence），且本轮台账存在「截断」条目，且审计点名 token 全部存在
     于全量语料 → 审计盲区而非草稿缺陷 → 直接 accept（不再重生成）。
  R2 同族 stall cut：连续 N（默认 2，可配 [agent.audit] stall_cut_family_repeats，
     0=off）次同族审计失败 → 视同预算耗尽直接进入 salvage，不再整稿重发
     （生产 18 分钟长循环的硬界）。
  R3 不误放：npg/citation 来源、矛盾族、contradicted 状态、无 token、空证据、
     台账无截断条目 → R1 一律不触发（fail-closed）。

Run: cd backend && python -m pytest tests/test_audit_convergence_guard.py -v
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import get_config
from app.services.agent_loop import (
    AgentLoop,
    AgentLoopState,
    ToolCallResult,
    _audit_family_stalled,
    _audit_problem_family,
    _visibility_accept_gate,
)

config = get_config()

USER_MSG = "你联网检索看看，尤其是看看arxiv上的对应论文，是不是大多数agent自进化论文都有我说的问题？"

# 生产形态：草稿引用论文具体数值，证据语料超审计可见预算（被截断）
DRAFT = (
    "RSEA 在六个方法四个基准上平均提升 70.7%，SEA-Eval 的 token 消耗约为 7 倍，"
    "VaG 跨 4 个骨干网络验证了这一点，SEAL 的密封审计也支持该结论。"
)
# 证据必须超出审计可见预算（_audit_evidence_budget 下限 4000 tokens ≈ 5700 CJK
# 字符），台账才会出现「截断」条目 —— 生产形态的受控复现。
_FILLER = "补充资料：以下为检索到的论文摘要与背景段落，用于占位使证据超出审计可见预算。" * 150
EVIDENCE_WITH_TOKENS = (
    "RSEA (2026-06) 在六个方法四个基准上平均提升 70.7%（绝对提升 0.136）。"
    "SEA-Eval 的 token 消耗约为 7 倍（相比 baseline）。"
    "VaG 跨 4 个骨干网络（Llama/Qwen/DeepSeek/Mistral）验证了自进化的收益边界。"
    "SEAL 采用密封审计（sealed audit）协议。"
) + _FILLER
EVIDENCE_WITHOUT_TOKENS = (
    "这是一段无关的检索结果：agent 自进化综述讨论了方法分类与评测基准，"
    "但没有给出任何具体提升数字或骨干网络数量。"
) + _FILLER

TRUNC_CLAIMS = [{"claim": "RSEA 平均提升 70.7%，VaG 跨 4 个骨干", "evidence_status": "truncated"}]
UNVERIFIABLE_JSON = (
    '{"ok": false, "verdict": "unverifiable", '
    '"unsupported_claims": [{"claim": "RSEA 平均提升 70.7%，VaG 跨 4 个骨干", '
    '"evidence_status": "truncated"}], '
    '"problem": "草稿具体数值落在截断证据范围内无法核对"}'
)
UNVERIFIABLE_MISSING_JSON = (
    '{"ok": false, "verdict": "unverifiable", '
    '"unsupported_claims": [{"claim": "RSEA 平均提升 99.9%", "evidence_status": "truncated"}], '
    '"problem": "草稿具体数值落在截断证据范围内无法核对"}'
)


class ScriptedAuditor:
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


def _make_state(evidence_text=EVIDENCE_WITH_TOKENS):
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    state.scout_used = True  # 跳过 scout aux 调用（本文件不测侦察）
    state.tool_results = [
        ToolCallResult(call_id="c1", name="browser", arguments={}, result=evidence_text),
    ]
    return state


def _run_audit(loop, state, draft, max_evidence_tokens=4000):
    """低证据预算 → 台账出现「截断」条目（生产形态的受控复现）；NPG 关闭
    （本文件不测数值闸门，避免测试草稿数值干扰）。**分段核对关闭**——本文件
    专测存在性闸门路径；分段核对路径见 test_segmented_claim_verify.py。"""
    old = _set_audit_cfg("max_evidence_tokens", max_evidence_tokens)
    old_npg = _set_audit_cfg("numeric_provenance_gate_mode", "off")
    old_seg = _set_audit_cfg("segmented_verify_enabled", False)
    try:
        return asyncio.run(loop._audit_response(state, draft, USER_MSG))
    finally:
        _restore_audit_cfg("max_evidence_tokens", old)
        _restore_audit_cfg("numeric_provenance_gate_mode", old_npg)
        _restore_audit_cfg("segmented_verify_enabled", old_seg)


# ── R1a: 问题族分类（确定性） ────────────────────────────────────────────────

def test_family_truncation():
    assert _audit_problem_family(
        "llm", "草稿数值落在截断证据范围内无法核对") == "truncation"
    assert _audit_problem_family(
        "llm", "大量具体数据在可见证据中无对应") == "truncation"


def test_family_contradiction_priority():
    assert _audit_problem_family(
        "llm", "草稿与台账 calculate 结果矛盾，且属悬空引用") == "contradiction"


def test_family_unsupported():
    assert _audit_problem_family(
        "llm", "草稿凭空编造了搜索结果") == "unsupported"


def test_family_self_contained():
    assert _audit_problem_family(
        "llm", "草稿以澄清形式指代用户看不到的上一轮回答") == "self_contained"


def test_family_source_wins():
    assert _audit_problem_family("npg", "推导数值无回执") == "npg"
    assert _audit_problem_family("citation", "通篇无引用") == "citation"


def test_family_other():
    assert _audit_problem_family("llm", "请调用工具补充证据后再答") == "other"


# ── R1b: 可见性接收闸门（纯函数） ────────────────────────────────────────────

_LEDGER_TRUNC = "<evidence-ledger>\n[1] browser — 120000 tokens — 截断(超出预算，未展示)\n</evidence-ledger>"
_LEDGER_FULL = "<evidence-ledger>\n[1] browser — 120 tokens — 完整\n</evidence-ledger>"


def test_gate_unverifiable_all_tokens_present():
    assert _visibility_accept_gate(
        "unverifiable", "llm", "草稿具体数值落在截断证据范围内无法核对",
        TRUNC_CLAIMS, EVIDENCE_WITH_TOKENS, _LEDGER_TRUNC,
    ) is True


def test_gate_unverifiable_token_missing():
    assert _visibility_accept_gate(
        "unverifiable", "llm", "草稿具体数值落在截断证据范围内无法核对",
        [{"claim": "RSEA 平均提升 99.9%", "evidence_status": "truncated"}],
        EVIDENCE_WITH_TOKENS, _LEDGER_TRUNC,
    ) is False


def test_gate_needs_evidence_truncation_marker():
    assert _visibility_accept_gate(
        "needs_evidence", "llm", "草稿大量具体数据在可见证据中无对应，需核对",
        TRUNC_CLAIMS, EVIDENCE_WITH_TOKENS, _LEDGER_TRUNC,
    ) is True


def test_gate_needs_evidence_non_visibility_problem():
    """非可见性族 needs_evidence（如要求补调工具）不得被接收。"""
    assert _visibility_accept_gate(
        "needs_evidence", "llm", "请立即调用工具补充证据后再作答",
        TRUNC_CLAIMS, EVIDENCE_WITH_TOKENS, _LEDGER_TRUNC,
    ) is False


def test_gate_excludes_contradiction():
    assert _visibility_accept_gate(
        "unverifiable", "llm", "草稿数值与台账矛盾，且落在截断证据范围内",
        TRUNC_CLAIMS, EVIDENCE_WITH_TOKENS, _LEDGER_TRUNC,
    ) is False
    assert _visibility_accept_gate(
        "unverifiable", "llm", "落在截断证据范围内无法核对",
        [{"claim": "RSEA 平均提升 70.7%", "evidence_status": "contradicted"}],
        EVIDENCE_WITH_TOKENS, _LEDGER_TRUNC,
    ) is False


def test_gate_excludes_npg_citation_source():
    assert _visibility_accept_gate(
        "needs_evidence", "npg", "推导数值无回执",
        TRUNC_CLAIMS, EVIDENCE_WITH_TOKENS, _LEDGER_TRUNC,
    ) is False
    assert _visibility_accept_gate(
        "needs_evidence", "citation", "本轮已检索但草稿通篇无 [N]",
        TRUNC_CLAIMS, EVIDENCE_WITH_TOKENS, _LEDGER_TRUNC,
    ) is False


def test_gate_fail_closed_without_tokens_or_evidence_or_truncation():
    # 无 token
    assert _visibility_accept_gate(
        "unverifiable", "llm", "无法核对", [], EVIDENCE_WITH_TOKENS, _LEDGER_TRUNC,
    ) is False
    # 空证据
    assert _visibility_accept_gate(
        "unverifiable", "llm", "无法核对", TRUNC_CLAIMS, "", _LEDGER_TRUNC,
    ) is False
    # 台账无截断条目（无可见性限制 → 非本闸门目标）
    assert _visibility_accept_gate(
        "unverifiable", "llm", "无法核对", TRUNC_CLAIMS, EVIDENCE_WITH_TOKENS, _LEDGER_FULL,
    ) is False


def test_gate_excludes_reject_verdict():
    assert _visibility_accept_gate(
        "reject", "llm", "草稿与台账矛盾", TRUNC_CLAIMS, EVIDENCE_WITH_TOKENS, _LEDGER_TRUNC,
    ) is False


def test_gate_citation_refs_stripped_from_parenthetical():
    """生产问题文本「（[7][10][12][15]等）」只有引用编号 → 提不出声称 token
    → fail-closed（编号有效性由 CitationLedger 单独把关，不得当接地 token）。"""
    assert _visibility_accept_gate(
        "needs_evidence", "llm",
        "草稿大量具体数字/公式落在截断证据（[7][10][12][15]等）范围内无法核对",
        [], EVIDENCE_WITH_TOKENS, _LEDGER_TRUNC,
    ) is False
    # 同一问题带真实声称 token（全在语料）→ 接收
    assert _visibility_accept_gate(
        "needs_evidence", "llm",
        "草稿具体数值（RSEA 70.7%、VaG 跨 4 骨干）落在截断证据范围内无法核对",
        [], EVIDENCE_WITH_TOKENS, _LEDGER_TRUNC,
    ) is True


# ── R2: 同族 stall cut（纯函数） ─────────────────────────────────────────────

def test_stall_two_consecutive_same_family():
    assert _audit_family_stalled(["truncation"], 2) is False
    assert _audit_family_stalled(["truncation", "truncation"], 2) is True
    assert _audit_family_stalled(["unsupported", "unsupported"], 2) is True


def test_stall_alternating_families_no_cut():
    assert _audit_family_stalled(["truncation", "npg"], 2) is False
    assert _audit_family_stalled(["truncation", "npg", "truncation"], 2) is False


def test_stall_zero_disables():
    assert _audit_family_stalled(["truncation", "truncation", "truncation"], 0) is False


def test_stall_configurable_repeats():
    assert _audit_family_stalled(["unsupported", "unsupported"], 3) is False
    assert _audit_family_stalled(["unsupported", "unsupported", "unsupported"], 3) is True


# ── R1c: _audit_response 集成（生产形态） ─────────────────────────────────────

def test_audit_response_accepts_visibility_limited_draft():
    """生产事故形态：审计说「截断证据无法核对」，但点名 token 全在全量语料
    → 直接 accept（返回 None），不再重生成（旧行为：needs_evidence → 循环）。"""
    loop = make_loop([UNVERIFIABLE_JSON])
    state = _make_state(EVIDENCE_WITH_TOKENS)
    verdict = _run_audit(loop, state, DRAFT)
    assert verdict is None, "被点名 token 全在语料中 → 审计盲区，必须接收"


def test_audit_response_keeps_unverifiable_when_token_absent():
    """token 不在语料中（真实缺失）→ 闸门不触发，判决原样返回（重生成路径）。"""
    loop = make_loop([UNVERIFIABLE_MISSING_JSON])
    state = _make_state(EVIDENCE_WITHOUT_TOKENS)
    verdict = _run_audit(loop, state, DRAFT)
    assert verdict is not None and verdict.verdict == "unverifiable", verdict


def test_audit_response_ledger_full_no_gate():
    """台账无截断（证据完整可见）→ 闸门不触发（无可见性限制，保持既有语义）。"""
    loop = make_loop([UNVERIFIABLE_JSON])
    state = _make_state(EVIDENCE_WITH_TOKENS)
    verdict = _run_audit(loop, state, DRAFT, max_evidence_tokens=1000000)
    assert verdict is not None and verdict.verdict == "unverifiable", verdict


# ── A4.9 R1 修复回归（2026-09-15 晚，评审 Important-1/2/4 + Minor-5/7） ──────

def test_stall_exempts_npg_and_other():
    """Important-1/Minor-7：npg 由 churn 感知 no-progress 守卫管辖（flags 下降/
    新工具调用=仍在收敛），other 无法归类（两次 unrelated 不构成同族）——
    族级连击不得覆盖/误伤，两者豁免。"""
    assert _audit_family_stalled(["npg", "npg"], 2) is False
    assert _audit_family_stalled(["other", "other"], 2) is False
    assert _audit_family_stalled(["truncation", "npg", "npg"], 2) is False
    assert _audit_family_stalled(["truncation", "truncation"], 2) is True


def test_gate_excludes_self_contained():
    """Minor-5：悬空指代族（规则 7）不得被 unverifiable 通道接收。"""
    assert _visibility_accept_gate(
        "unverifiable", "llm",
        "草稿以澄清形式指代用户看不到的上一轮回答，无法核对",
        TRUNC_CLAIMS, EVIDENCE_WITH_TOKENS, _LEDGER_TRUNC,
    ) is False


def test_gate_ascii_numunit_tightening():
    """Important-2 部分收紧：ASCII 单位/百分号数值（表示稳定）纳入必查 token；
    CJK 单位与拼写数字不做跨表示匹配（实证："7 倍" 在语料中写作 "sevenfold"，
    严格匹配会误杀正确声称）。"""
    # 5% 不在语料 → fail-closed（ASCII 单位数值必须可查）
    assert _visibility_accept_gate(
        "unverifiable", "llm", "草稿数值（消耗 5% 预算）落在截断证据范围内无法核对",
        [], EVIDENCE_WITH_TOKENS, _LEDGER_TRUNC,
    ) is False
    # 70.7% 在语料 → 接收
    assert _visibility_accept_gate(
        "unverifiable", "llm", "草稿数值（平均提升 70.7%）落在截断证据范围内无法核对",
        [], EVIDENCE_WITH_TOKENS, _LEDGER_TRUNC,
    ) is True
    # 混合声称（主通道非空 + 个位数单位值）——通道存在性判别用例（A4.9 R2
    # Minor-1）：去掉 ASCII 通道后此断言翻转为 True（假接收），是唯一能判别
    # 「通道仍生效」的用例。
    assert _visibility_accept_gate(
        "unverifiable", "llm", "草稿数值（RSEA 消耗 5% 预算）落在截断证据范围内无法核对",
        [], EVIDENCE_WITH_TOKENS, _LEDGER_TRUNC,
    ) is False


def test_gate_kill_switch_disables():
    """Important-4：visibility_accept_enabled=false 一键回退旧行为。"""
    loop = make_loop([UNVERIFIABLE_JSON])
    state = _make_state(EVIDENCE_WITH_TOKENS)
    old = _set_audit_cfg("visibility_accept_enabled", False)
    try:
        verdict = _run_audit(loop, state, DRAFT)
    finally:
        _restore_audit_cfg("visibility_accept_enabled", old)
    assert verdict is not None and verdict.verdict == "unverifiable", verdict


def test_main_loop_wiring_present():
    """Q4(a)：主循环 stall cut 接线的静态回归（防意外移除）。"""
    with open(os.path.join(os.path.dirname(__file__), "..", "app", "services", "agent_loop.py"), "r", encoding="utf-8") as f:
        src = f.read()
    with open(os.path.join(os.path.dirname(__file__), "..", "app", "services", "agent_loop_orchestrator.py"), "r", encoding="utf-8") as f:
        src += f.read()
    # C-01c: `_audit_family` now lives on `_LoopResult` (verdict helper).
    assert "state.audit_fail_families.append(_loop_result._audit_family)" in src
    assert "_family_stall = _audit_family_stalled(" in src
    assert "or _loop_result._family_stall" in src
    assert "Audit family stall cut" in src
