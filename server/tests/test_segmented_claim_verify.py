"""RED tests (A4.8) — 分段声称核对（segmented claim verification，2026-09-15）。

用户拍板落地（调研：memory/reference_subagent_segmented_audit_20260915.md）。
SOTA：AgentAuditor 2602.09341（局部证据包 -44.8% tokens）· 验证优先双代理
（独立上下文）· MAVEN 2605.07646（生成/验证解耦）· MAV 2502.20379。

契约：
  R1 map（确定性，零 LLM）：草稿句子 → 不可见 token（在全量语料存在、在
     显示窗不可见）→ token → ±600 字符 span 窗口 → 一包一声称。
  R2 verify（并行轻量调用，独立上下文）：每包只带声称 + span 切片；
     输出 pass / fail / unverifiable。
  R3 reduce（确定性）：任一 fail → **reject**（source=claim_verify，点名声称+
     理由，copy-edit 指导语；A4.9 R1 Important-3：软判决会被兜底选稿 soft 优先
     规则选中出货）；无 fail → 闸门判定接收（走 citation remand 检查）；
     全包调用失败/超时/未启用/无不可见声称 → 回退存在性闸门（fail-open）。
  R0 组合纪律（A4.9 R1 Critical-1）：**先闸门后分段**——存在性闸门（审计点名
     token 全在场）通过后才跑分段核对，分段只做否决（不引入更宽接收面）。

Run: cd backend && python -m pytest tests/test_segmented_claim_verify.py -v
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
    _aggregate_claim_verdicts,
    _build_claim_packs,
    _claim_tokens_with_units,
    _draft_claim_sentences,
    _find_token_spans,
    _parse_claim_verify,
    _select_invisible_claims,
)

config = get_config()

USER_MSG = "你联网检索看看，尤其是看看arxiv上的对应论文，是不是大多数agent自进化论文都有我说的问题？"

# 显示窗不可见 token 的载体（生产形态）：全量语料 > 审计可见预算（4000）→
# 条目退化为 800 字符头尾片段（_tool_evidence_fragment），token 藏在**中段**
# ——头部/尾部填充必须足够长，否则 token 会落进头尾片段而「可见」。
_HEAD_PAD = "前言：" + "甲" * 600
_TAIL_PAD = "后记：" + "乙" * 4200
EVIDENCE_WITH_TOKENS = (
    _HEAD_PAD
    + "RSEA (2026-06) 在六个方法四个基准上平均提升 70.7%（绝对提升 0.136）。"
    + "SEA-Eval 的 token 消耗约为 7 倍（相比 baseline）。"
    + "VaG 跨 4 个骨干网络（Llama/Qwen/DeepSeek/Mistral）验证了自进化的收益边界。"
    + _TAIL_PAD
)

DRAFT = (
    "RSEA 在六个方法四个基准上平均提升 70.7%，SEA-Eval 的 token 消耗约为 7 倍，"
    "VaG 跨 4 个骨干网络验证了这一点。"
)

UNVERIFIABLE_JSON = (
    '{"ok": false, "verdict": "unverifiable", "unsupported_claims": [], '
    '"problem": "草稿具体数值落在截断证据范围内无法核对"}'
)
# 带点名声称的判决——供「回退存在性闸门」用例（闸门从 claims 提 token）
UNVERIFIABLE_WITH_CLAIMS_JSON = (
    '{"ok": false, "verdict": "unverifiable", '
    '"unsupported_claims": [{"claim": "RSEA 平均提升 70.7%", '
    '"evidence_status": "truncated"}], '
    '"problem": "草稿具体数值落在截断证据范围内无法核对"}'
)
VERIFY_PASS = '{"verdict": "pass", "reason": "证据片段含 70.7%，与声称一致"}'
VERIFY_FAIL = '{"verdict": "fail", "reason": "片段中该指标为 68.2%，与声称的 70.7% 矛盾"}'
VERIFY_UNVERIFIABLE = '{"verdict": "unverifiable", "reason": "片段未覆盖该声称"}'
VERIFY_BAD = "我不知道怎么核对"


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
    loop.audit_llm = object()  # sentinel：验证调用透传该客户端
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
    state.scout_used = True
    state.tool_results = [
        ToolCallResult(call_id="c1", name="browser", arguments={}, result=evidence_text),
    ]
    return state


def _run_audit(loop, state, draft, max_evidence_tokens=4000):
    old = _set_audit_cfg("max_evidence_tokens", max_evidence_tokens)
    old_npg = _set_audit_cfg("numeric_provenance_gate_mode", "off")
    try:
        return asyncio.run(loop._audit_response(state, draft, USER_MSG))
    finally:
        _restore_audit_cfg("max_evidence_tokens", old)
        _restore_audit_cfg("numeric_provenance_gate_mode", old_npg)


# ── R1a: 句子切分与 token 提取 ──────────────────────────────────────────────

def test_draft_claim_sentences_split_and_cap():
    sents = _draft_claim_sentences("第一句。第二句！第三句？\n第四句；第五句")
    assert sents == ["第一句。", "第二句！", "第三句？", "第四句；", "第五句"]
    long = "长" * 600
    assert len(_draft_claim_sentences(long)[0]) == 500


def test_claim_tokens_with_units():
    toks = _claim_tokens_with_units("RSEA 平均提升 70.7%，消耗 5%，见 [7][10]。")
    assert "RSEA" in toks
    assert "70.7" in toks          # 主通道
    assert "5%" in toks            # ASCII 单位通道（个位数）
    assert "10" not in toks        # [N] 引用编号剥离
    assert "7" not in toks         # 单数字不带单位不可见


# ── R1b: 不可见声称选择 ─────────────────────────────────────────────────────

def test_select_invisible_claims():
    display = "RSEA (2026-06) 在六个方法四个基准上平均提升 70.7%（绝对提升 0.136）。"
    full = display + " VaG 跨 4 个骨干网络验证。"
    claims, total = _select_invisible_claims("RSEA 提升 70.7%。VaG 跨 4 个骨干。", display, full)
    assert len(claims) == 1 and total == 1, claims
    sent, toks = claims[0]
    assert "VaG" in sent and "VaG" in toks
    assert "70.7" not in toks      # 显示窗可见 → 不进分段核对


def test_select_invisible_claims_absent_token_not_selected():
    claims, total = _select_invisible_claims("虚构指标 99.9 不存在。", "", "语料中没有这个数。")
    assert claims == [] and total == 0


# ── R1c: span 定位与打包 ────────────────────────────────────────────────────

def test_find_token_spans_window_and_missing():
    full = "A" * 800 + " 70.7 " + "B" * 800
    spans = _find_token_spans(full, ["70.7", "缺失token"])
    assert len(spans) == 1
    assert "70.7" in spans[0]
    assert 1000 <= len(spans[0]) <= 1210


def test_find_token_spans_prefers_cooccurrence():
    """A6 真实调用观察：token 在无关段落出现时，选「窗口内共现其他声称 token
    最多」的命中（首个命中会把无关窗口喂给验证器 → 只能判 unverifiable）。"""
    unrelated = "locked 出现在无关段落，" + "x" * 800 + "。"
    related = "Who Grades the Grader 的 locked set 与 anchor 回归。"
    spans = _find_token_spans(unrelated + related, ["locked", "Grader"])
    assert any("Grader" in sp for sp in spans), spans


def test_find_token_spans_dedupes_overlap():
    full = "X" * 100 + "70.7 和 0.136 同段" + "Y" * 100
    spans = _find_token_spans(full, ["70.7", "0.136"])
    assert len(spans) == 1  # 两个窗口几乎重合 → 去重


def test_build_claim_packs_order_cap_and_budget():
    full = ("RSEA 70.7%。" + "f" * 4000 + " VaG 4 骨干。SEAL 密封。")
    claims = [
        ("VaG 跨 4 个骨干。", ["VaG"]),
        ("RSEA 提升 70.7%。", ["70.7", "RSEA"]),
        ("SEAL 密封。", ["SEAL"]),
    ]
    packs = _build_claim_packs(claims, full, max_packs=2, pack_tokens=1000)
    assert len(packs) == 2
    assert "RSEA" in packs[0].claim          # token 多者优先
    assert packs[0].tokens == ["70.7", "RSEA"]


# ── R2/R3: 解析与聚合 ───────────────────────────────────────────────────────

def test_parse_claim_verify():
    assert _parse_claim_verify(VERIFY_PASS) == ("pass", "证据片段含 70.7%，与声称一致")
    assert _parse_claim_verify('```json\n{"verdict": "fail", "reason": "x"}\n```') == ("fail", "x")
    assert _parse_claim_verify(VERIFY_BAD) is None
    assert _parse_claim_verify('{"verdict": "maybe"}') is None
    assert _parse_claim_verify("") is None


def test_aggregate_claim_verdicts():
    claims = ["c1", "c2", "c3"]
    ok = _aggregate_claim_verdicts(
        [("pass", ""), ("unverifiable", ""), ("pass", "")], claims)
    assert ok.accept is True and ok.failed == []
    bad = _aggregate_claim_verdicts(
        [("pass", ""), ("fail", "数值矛盾"), None], claims)
    assert bad.accept is False
    assert bad.failed == [("c2", "数值矛盾")]
    assert bad.failed_calls == 1
    allfail = _aggregate_claim_verdicts([None, None], ["c1", "c2"])
    assert allfail.accept is True and allfail.failed_calls == 2


# ── R1-R3 集成（_audit_response，生产形态） ─────────────────────────────────

def test_audit_response_claim_verify_pass_accepts(caplog):
    loop = make_loop([UNVERIFIABLE_WITH_CLAIMS_JSON, VERIFY_PASS, VERIFY_PASS, VERIFY_PASS])
    state = _make_state()
    with caplog.at_level("INFO", logger="app.services.agent_loop"):
        verdict = _run_audit(loop, state, DRAFT)
    assert verdict is None, "验证通过 → 接收"
    assert any("outcome=accept_claim_verify" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]


def test_audit_response_claim_verify_fail_returns_reject():
    """A4.9 R1 Important-3：验证器见到证据矛盾 = 硬信号，必须 reject——
    软判决会被 deterministic 兜底选稿的「soft 优先」规则选中出货。"""
    loop = make_loop([UNVERIFIABLE_WITH_CLAIMS_JSON, VERIFY_FAIL])
    state = _make_state()
    verdict = _run_audit(loop, state, DRAFT)
    assert verdict is not None
    assert verdict.verdict == "reject"
    assert verdict.source == "claim_verify"
    assert "矛盾" in verdict.problem
    assert "逐字保留" in verdict.guidance


def test_audit_response_claim_verify_disabled_falls_back_to_gate(caplog):
    loop = make_loop([UNVERIFIABLE_WITH_CLAIMS_JSON])
    state = _make_state()
    old = _set_audit_cfg("segmented_verify_enabled", False)
    try:
        with caplog.at_level("INFO", logger="app.services.agent_loop"):
            verdict = _run_audit(loop, state, DRAFT)
    finally:
        _restore_audit_cfg("segmented_verify_enabled", old)
    assert verdict is None, "关闭分段核对 → 存在性闸门接收（旧行为）"
    assert any("outcome=accept_visibility" in r.message for r in caplog.records)


def test_audit_response_claim_verify_all_calls_fail_falls_back(caplog):
    loop = make_loop([UNVERIFIABLE_WITH_CLAIMS_JSON, VERIFY_BAD, VERIFY_BAD, VERIFY_BAD])
    state = _make_state()
    with caplog.at_level("INFO", logger="app.services.agent_loop"):
        verdict = _run_audit(loop, state, DRAFT)
    assert verdict is None, "全部验证调用失败 → 回退存在性闸门（fail-open）"
    assert any("outcome=accept_visibility" in r.message for r in caplog.records)
    # W2（2026-09-16）：基础设施失败 = degraded —— 不再静默，置低置信标记
    assert any("degraded=True" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]
    assert state.audit_low_confidence is True

# ── A4.9 R1 修复回归 ────────────────────────────────────────────────────────

def test_absent_token_claim_blocks_accept(caplog):
    """Critical-1（veto 组合）：审计点名 claims 含「全量语料也没有」的 token
    （12.3）→ 存在性闸门 fail-closed（全 token 在场要求）→ 分段核对不跑、
    不接收 → 缺席 token 绝不被静默原谅（与旧路径一致）。"""
    evidence = _HEAD_PAD + "VaG 跨 4 个骨干网络验证了收益边界。" + _TAIL_PAD
    draft = "VaG 跨 4 个骨干网络把准确率提升到 12.3%。"
    loop = make_loop([json.dumps({
        "ok": False, "verdict": "unverifiable",
        "unsupported_claims": [{"claim": "准确率提升到 12.3%", "evidence_status": "truncated"}],
        "problem": "草稿具体数值落在截断证据范围内无法核对",
    }, ensure_ascii=False)])
    state = _make_state(evidence)
    with caplog.at_level("INFO", logger="app.services.agent_loop"):
        verdict = _run_audit(loop, state, draft)
    assert not any("accept_claim_verify" in r.message for r in caplog.records)
    assert not any("accept_visibility" in r.message for r in caplog.records)
    assert verdict is not None and verdict.verdict == "unverifiable", \
        "缺席 token（12.3）不得被静默原谅"


def test_contradicted_status_skips_segmented(caplog):
    """Important-2：审计 claims 带 contradicted 状态 → 不进入分段接收。"""
    loop = make_loop([json.dumps({
        "ok": False, "verdict": "unverifiable",
        "unsupported_claims": [{"claim": "RSEA 提升 70.7%", "evidence_status": "contradicted"}],
        "problem": "草稿具体数值落在截断证据范围内无法核对",
    }, ensure_ascii=False), VERIFY_PASS])
    state = _make_state()
    with caplog.at_level("INFO", logger="app.services.agent_loop"):
        verdict = _run_audit(loop, state, DRAFT)
    assert not any("accept_claim_verify" in r.message for r in caplog.records)
    assert verdict is not None and verdict.verdict == "unverifiable"


def test_self_contained_problem_skips_segmented(caplog):
    """Important-2：悬空指代族问题文本 → 不进入分段接收（存在性闸门护栏）。"""
    loop = make_loop([json.dumps({
        "ok": False, "verdict": "unverifiable", "unsupported_claims": [],
        "problem": "草稿以澄清形式指代用户看不到的上一轮回答，且证据被截断无法核对",
    }, ensure_ascii=False)])
    state = _make_state()
    with caplog.at_level("INFO", logger="app.services.agent_loop"):
        verdict = _run_audit(loop, state, DRAFT)
    assert not any("accept_claim_verify" in r.message for r in caplog.records)
    assert verdict is not None


def test_phantom_token_filtered():
    from app.services.agent_loop import _is_phantom_token
    assert _is_phantom_token("06", "论文 2026-06 发布") is True
    assert _is_phantom_token("42", "共 42 个样本") is False
    assert _is_phantom_token("RSEA", "2026-06") is False


def test_sentence_split_ascii_period_and_hard_wrap():
    sents = _draft_claim_sentences("First claim 70.7 percent. Second claim VaG.")
    assert len(sents) == 2, sents
    long = "甲" * 1200
    parts = _draft_claim_sentences(long)
    assert len(parts) == 3 and sum(len(p) for p in parts) == 1200


def test_select_invisible_claims_deterministic_order():
    display = ""
    full = "VaG 跨 4 个骨干。RSEA 提升 70.7%。SEA-Eval 消耗 7 倍。"
    claims, _total = _select_invisible_claims(
        "RSEA 提升 70.7%，SEA-Eval 消耗 7 倍，VaG 跨 4 个骨干。", display, full)
    assert len(claims) == 1
    _sent, toks = claims[0]
    assert toks == sorted(toks), toks


def test_zero_packs_disables():
    loop = make_loop([UNVERIFIABLE_WITH_CLAIMS_JSON, VERIFY_PASS])
    state = _make_state()
    old = _set_audit_cfg("segmented_verify_max_packs", 0)
    try:
        verdict = _run_audit(loop, state, DRAFT)
    finally:
        _restore_audit_cfg("segmented_verify_max_packs", old)
    assert verdict is None  # 0=关闭 → 存在性闸门接收（旧行为）

def test_claim_verify_timeout_falls_back(caplog):
    """A4.9 R2 Minor：聚合总超时（segmented_verify_timeout_seconds）→ 回退
    存在性闸门（不阻塞轮次、不泄漏子任务）。"""
    import asyncio as _aio

    class SlowAuditor(ScriptedAuditor):
        async def __call__(self, messages, temperature=None, max_tokens=None, **kw):
            self.calls.append(list(messages))
            if self.results:
                return self.results.pop(0)
            await _aio.sleep(0.2)  # 模拟挂起的验证调用
            return VERIFY_PASS

    loop = make_loop()
    loop.auditor = SlowAuditor([UNVERIFIABLE_WITH_CLAIMS_JSON])
    loop._complete_json = loop.auditor
    state = _make_state()
    old = _set_audit_cfg("segmented_verify_timeout_seconds", 0.05)
    try:
        with caplog.at_level("INFO", logger="app.services.agent_loop"):
            verdict = _run_audit(loop, state, DRAFT)
    finally:
        _restore_audit_cfg("segmented_verify_timeout_seconds", old)
    assert any("claim_verify timed out" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]
    assert verdict is None  # 超时回退存在性闸门接收
    assert any("accept_visibility" in r.message for r in caplog.records)
    # W2：超时 = degraded 基础设施失败 → 低置信标记（不再静默）
    assert state.audit_low_confidence is True


def test_claim_verify_map_exception_falls_back(caplog, monkeypatch):
    """A4.9 R2 Minor：map 异常不得破坏审计 fail-open 契约 → 回退闸门。"""
    import app.services.agent_loop as _al

    def _boom(*a, **kw):
        raise RuntimeError("map exploded")

    monkeypatch.setattr(_al, "_select_invisible_claims", _boom)
    loop = make_loop([UNVERIFIABLE_WITH_CLAIMS_JSON])
    state = _make_state()
    with caplog.at_level("INFO", logger="app.services.agent_loop"):
        verdict = _run_audit(loop, state, DRAFT)
    assert any("claim_verify map failed" in r.message for r in caplog.records)
    assert verdict is None
    assert any("accept_visibility" in r.message for r in caplog.records)
    # W2：map 异常 = degraded 基础设施失败 → 低置信标记（不再静默）
    assert state.audit_low_confidence is True


def test_zero_pack_tokens_disables(caplog):
    loop = make_loop([UNVERIFIABLE_WITH_CLAIMS_JSON, VERIFY_PASS])
    state = _make_state()
    old = _set_audit_cfg("segmented_verify_pack_tokens", 0)
    try:
        with caplog.at_level("INFO", logger="app.services.agent_loop"):
            verdict = _run_audit(loop, state, DRAFT)
    finally:
        _restore_audit_cfg("segmented_verify_pack_tokens", old)
    assert verdict is None
    assert any("accept_visibility" in r.message for r in caplog.records)


# ── W2（2026-09-16）：degraded 标记 / 覆盖计数 / 宽窗重试 / 低置信提示 ──────
# 背景：C4 评估（memory/reference_audit_visibility_gate_eval_20260916.md）残余
# 风险处理——fail-open 链不再静默（degraded → 低置信标记 + 出货末尾说明）；
# 覆盖上限外声称计数并同样置标记；unverifiable 包以更宽证据窗口重试一次。

DRAFT_TWO_CLAIMS = (
    "RSEA 在六个方法四个基准上平均提升 70.7%。VaG 跨 4 个骨干网络验证了收益边界。"
)


def test_select_invisible_claims_returns_total_and_cap():
    """签名扩展：(claims, total)——total 含被 max_claims 截掉的候选声称。"""
    display = ""
    full = "VaG 跨 4 个骨干。RSEA 提升 70.7%。"
    claims, total = _select_invisible_claims(
        "RSEA 提升 70.7%。VaG 跨 4 个骨干。", display, full, max_claims=1)
    assert total == 2 and len(claims) == 1


def test_uncovered_claims_flag_low_confidence(caplog):
    """覆盖上限（max_packs=1 < 候选 2）→ 接收但置低置信标记 + uncovered 日志。"""
    loop = make_loop([UNVERIFIABLE_WITH_CLAIMS_JSON, VERIFY_PASS])
    state = _make_state()
    old = _set_audit_cfg("segmented_verify_max_packs", 1)
    try:
        with caplog.at_level("INFO", logger="app.services.agent_loop"):
            verdict = _run_audit(loop, state, DRAFT_TWO_CLAIMS)
    finally:
        _restore_audit_cfg("segmented_verify_max_packs", old)
    assert verdict is None
    assert any(
        "accept_claim_verify" in r.message and "uncovered=1" in r.message
        for r in caplog.records), [r.message for r in caplog.records]
    assert state.audit_low_confidence is True


def test_uncovered_kill_switch_leaves_flag_clear(caplog):
    """low_confidence_note_enabled=false → 覆盖缺口仍记日志但不置出货标记。"""
    loop = make_loop([UNVERIFIABLE_WITH_CLAIMS_JSON, VERIFY_PASS])
    state = _make_state()
    old1 = _set_audit_cfg("segmented_verify_max_packs", 1)
    old2 = _set_audit_cfg("low_confidence_note_enabled", False)
    try:
        with caplog.at_level("INFO", logger="app.services.agent_loop"):
            verdict = _run_audit(loop, state, DRAFT_TWO_CLAIMS)
    finally:
        _restore_audit_cfg("segmented_verify_max_packs", old1)
        _restore_audit_cfg("low_confidence_note_enabled", old2)
    assert verdict is None
    assert any("uncovered=1" in r.message for r in caplog.records)
    assert state.audit_low_confidence is False


def test_unverifiable_pack_retried_with_wider_window(caplog):
    """宽窗重试：首轮 unverifiable → 以更宽窗口重试一次，重试 pass → 接收；
    重试请求的证据切片显著变宽；覆盖完整（无 uncovered/degraded）→ 不置标记。"""
    loop = make_loop([UNVERIFIABLE_WITH_CLAIMS_JSON, VERIFY_UNVERIFIABLE, VERIFY_PASS])
    state = _make_state()
    with caplog.at_level("INFO", logger="app.services.agent_loop"):
        verdict = _run_audit(loop, state, DRAFT)
    assert verdict is None
    assert any("accept_claim_verify" in r.message for r in caplog.records)
    assert len(loop.auditor.calls) == 3  # 1 审计 + 首轮验证 + 宽窗重试
    first_msg = loop.auditor.calls[1][-1]["content"]
    retry_msg = loop.auditor.calls[2][-1]["content"]
    assert len(retry_msg) > len(first_msg), "重试应携带更宽的证据窗口"
    assert state.audit_low_confidence is False


def test_unverifiable_retry_disabled_by_zero_window():
    """segmented_verify_retry_window_chars=0 → 不重试（与兄弟开关 0=off 一致）。"""
    loop = make_loop([UNVERIFIABLE_WITH_CLAIMS_JSON, VERIFY_UNVERIFIABLE])
    state = _make_state()
    old = _set_audit_cfg("segmented_verify_retry_window_chars", 0)
    try:
        verdict = _run_audit(loop, state, DRAFT)
    finally:
        _restore_audit_cfg("segmented_verify_retry_window_chars", old)
    assert verdict is None
    assert len(loop.auditor.calls) == 2  # 审计 + 首轮验证，无重试


def test_low_confidence_note_wiring_and_config():
    """接线静态回归（同 R2 Minor-5 弱回归锁哲学）：提示文案常量 + 四个出货点
    （draft/synthesis/salvage/selection）+ 配置键默认值（无键旧部署=新行为）。

    2026-09-20 更新（分块审计波）：出货点改经 `_consume_low_confidence_note`
    统一消费（两类来源文案不同：存在性低置信 vs 分块覆盖不完整），仍锁 4 个点。
    """
    import inspect
    import app.services.agent_loop as _al
    src = inspect.getsource(_al)
    assert "_AUDIT_LOW_CONFIDENCE_NOTE" in src
    assert "_AUDIT_CHUNK_COVERAGE_NOTE" in src
    assert src.count("_consume_low_confidence_note(state)") >= 4, \
        "draft/synthesis/salvage/selection 四个出货点都要消费低置信/覆盖说明"
    assert "audit_low_confidence" in src
    assert "audit_chunk_coverage_incomplete" in src
    assert config.agent_audit_low_confidence_note_enabled is True
    assert config.agent_audit_chunked_claim_audit_enabled is True
    assert config.agent_audit_segmented_verify_retry_window_chars == 1500
    assert config.agent_audit_segmented_verify_max_claims == 40


# ── A4.9 R1 修复轮（2026-09-16）────────────────────────────────────────────

def test_draft_ships_as_final_predicate():
    """Important-1：非 reasoning / retry_thinking_off 路径草稿即最终稿（无合成
    消费点）——低置信说明必须在 draft 点消费，否则该类轮次静默出货。"""
    from app.services.agent_loop import _draft_ships_as_final
    assert _draft_ships_as_final(True, True, False) is True    # live 流式
    assert _draft_ships_as_final(False, False, False) is True  # 非 reasoning（无合成 pass）
    assert _draft_ships_as_final(False, True, True) is True    # 关思考重试有内容路径
    assert _draft_ships_as_final(False, True, False) is False  # 非 live + reasoning → 合成审计点判定


def test_draft_note_consumption_covers_non_reasoning_mode():
    """接线回归（Important-1）：draft 出货点的说明消费不再只挂 live 条件。"""
    import inspect
    import app.services.agent_loop as _al
    src = inspect.getsource(_al)
    assert "_draft_ships_as_final(" in src


def test_timeout_degraded_carries_uncovered_and_metric(caplog):
    """Minor-1/2：聚合超时的 degraded 保留覆盖信息（uncovered 进 claim_verify
    指标行与 accept_visibility outcome 行），不再丢失。"""
    import asyncio as _aio

    class SlowAuditor(ScriptedAuditor):
        async def __call__(self, messages, temperature=None, max_tokens=None, **kw):
            self.calls.append(list(messages))
            if self.results:
                return self.results.pop(0)
            await _aio.sleep(0.2)  # 模拟挂起的验证调用
            return VERIFY_PASS

    loop = make_loop()
    loop.auditor = SlowAuditor([UNVERIFIABLE_WITH_CLAIMS_JSON])
    loop._complete_json = loop.auditor
    state = _make_state()
    old = _set_audit_cfg("segmented_verify_timeout_seconds", 0.05)
    old2 = _set_audit_cfg("segmented_verify_max_packs", 1)
    try:
        with caplog.at_level("INFO", logger="app.services.agent_loop"):
            verdict = _run_audit(loop, state, DRAFT_TWO_CLAIMS)
    finally:
        _restore_audit_cfg("segmented_verify_timeout_seconds", old)
        _restore_audit_cfg("segmented_verify_max_packs", old2)
    assert verdict is None
    assert state.audit_low_confidence is True
    assert any(
        "claim_verify" in r.message and "degraded" in r.message
        for r in caplog.records), [r.message for r in caplog.records]
    assert any(
        "accept_visibility" in r.message and "uncovered=1" in r.message
        for r in caplog.records), [r.message for r in caplog.records]
