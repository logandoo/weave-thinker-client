"""RED tests (A4.8) — 2026-10-06 用户红线：审计草稿绝不允许截断（断章取义）。

起源：60 生产 conv ae9aa092 —— Q2 长研究稿被「没有引用」打回一次，但草稿中
引用实际存在。结构性根因：`_draft_audit_view` 头+尾窗口（>8k tokens 省略中段），
引用落在省略中段时审计员看不到 → 可能睁眼说「没有引用」。

契约（用户裁定，2026-10-06）：
  R1 审计 prompt 中草稿**逐字全文**——任何长度都不省略；效率手段只准用
     分块全覆盖审计（每块独立审计+保守合并），绝不允许「中间省略」。
  R2 超预算时降级顺序：先缩证据正文（既有契约：看不到→unverifiable 非 reject），
     草稿永不缩。
  R3 引用对照表直说「草稿实际使用的有效引用编号：[…]（共 N 个）」——确定性
     全文核对结论，不受任何窗口影响。
  R4 分块审计：每块标注第 i/N 块；合并优先级 reject>needs_evidence>unverifiable>
     accept；块调用失败显式置位（coverage incomplete），不得静默丢块。

Run: cd backend && python -m pytest tests/test_audit_full_draft.py -v
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import get_config
from app.services.agent_loop import AgentLoop, AgentLoopState, ToolCallResult
from app.services.citation_ledger import CitationLedger

config = get_config()

USER_MSG = "MOSS-Transcribe-Diarize 这个效果怎么样？对 rocm 支持效果如何？"


class ScriptedAuditor:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    async def __call__(self, messages, temperature=None, max_tokens=None, **kw):
        self.calls.append(list(messages))
        if self.results:
            r = self.results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
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


def _make_state(with_search=True, big_evidence=False):
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    if with_search:
        ledger = CitationLedger()
        hits = [
            {"url": f"https://example.com/src-{i}", "title": f"来源{i}",
             "snippet": f"摘要{i}"}
            for i in range(1, 13)
        ]
        ledger.register_hits(hits)
        state.citation_ledger = ledger
    results = [
        ToolCallResult(call_id="c1", name="web_search", arguments={},
                       result='{"results": [{"url": "https://example.com/src-1"}]}'),
    ]
    if big_evidence:
        results.append(ToolCallResult(
            call_id="c2", name="browser", arguments={},
            result="网页正文" * 30000))
    state.tool_results = results
    return state


def _audit_user_text(messages):
    assert len(messages) >= 2 and messages[1]["role"] == "user"
    return messages[1]["content"]


def _run_audit(loop, state, draft):
    old = _set_audit_cfg("numeric_provenance_gate_mode", "off")
    try:
        return asyncio.run(loop._audit_response(state, draft, USER_MSG))
    finally:
        _restore_audit_cfg("numeric_provenance_gate_mode", old)


# ── R3：引用对照表直说有效编号 ────────────────────────────────────────────

def test_mapping_view_lists_cited_ids():
    from app.services.agent_loop import _build_citation_mapping_view
    state = _make_state()
    draft = "转写第一梯队 [1]，独立复测 WER 约 4% [3]。"
    block = _build_citation_mapping_view(state, draft)
    assert "草稿实际使用的有效引用编号" in block, "对照表必须直说草稿实际引用了哪些有效编号"
    assert "[1]" in block and "[3]" in block
    assert "共 2 个" in block


def test_mapping_view_zero_cited_keeps_existing_line():
    from app.services.agent_loop import _build_citation_mapping_view
    state = _make_state()
    block = _build_citation_mapping_view(state, "没有任何引用的草稿。")
    assert "（草稿未使用引用编号）" in block
    assert "草稿实际使用的有效引用编号：" not in block


# ── R1：长草稿逐字全文，无省略 ────────────────────────────────────────────

def test_long_draft_audited_verbatim_full():
    loop = make_loop()
    state = _make_state()
    # ~6 万 CJK 字符（远超旧 8k token 窗口，远低于 128k 上限）
    head = "开头段落：转写基准表 [1]。"
    middle = "中段表格与论证 [2][3]，引用落在中段。" * 1500
    tail = "结尾段落：流式不支持 [4]。"
    draft = head + middle + tail
    verdict = _run_audit(loop, state, draft)
    assert verdict is None
    assert loop.auditor.calls, "审计调用必须发生"
    prompt = _audit_user_text(loop.auditor.calls[-1])
    assert draft in prompt, "草稿必须逐字全文进入审计 prompt"
    assert "中间省略" not in prompt, "绝不允许省略中段（断章取义）"
    assert "窗口为头+尾" not in prompt


# ── R2：超预算降级顺序——先证据，草稿永不缩 ──────────────────────────────

def test_over_budget_drops_evidence_never_draft():
    loop = make_loop()
    state = _make_state(big_evidence=True)
    # 窗口测算（estimate_text_tokens_rough：1 CJK 字符=1 token；杂项 ~675、证据体 ~980）：
    # 守卫触发需 draft>6345，降级后不触分块需 draft≤7325 → 取 ~6800。
    draft = ("中" * 6800) + " 结论引用 [1][2]。"
    keys = ["evidence_context_ceiling_tokens", "max_evidence_tokens"]
    olds = {k: _set_audit_cfg(k, 16000) for k in keys}  # budget = max(8000, 16000-8000) = 8000
    try:
        verdict = _run_audit(loop, state, draft)
    finally:
        for k, v in olds.items():
            _restore_audit_cfg(k, v)
    assert verdict is None
    prompt = _audit_user_text(loop.auditor.calls[-1])
    assert draft in prompt, "超预算时草稿也必须保持全文"
    assert "中间省略" not in prompt
    assert "证据正文因上下文上限未展开" in prompt, "降级必须先砍证据正文"


# ── R4：超巨型草稿分块全覆盖审计 ──────────────────────────────────────────

def _force_tiny_budget():
    keys = ["evidence_context_ceiling_tokens", "max_evidence_tokens"]
    olds = {k: _set_audit_cfg(k, 9000) for k in keys}  # budget = max(8000, 1000) = 8000
    return keys, olds


def test_gigantic_draft_chunked_full_coverage():
    loop = make_loop()
    state = _make_state()
    paras = [f"第{i:02d}段论点：引用 [{i % 12 + 1}]。" + "论据" * 400 for i in range(30)]
    draft = "\n\n".join(paras)  # 30 段 × ~800 字符，远超 8000 token 预算
    keys, olds = _force_tiny_budget()
    try:
        verdict = _run_audit(loop, state, draft)
    finally:
        for k, v in olds.items():
            _restore_audit_cfg(k, v)
    assert verdict is None
    n = len(loop.auditor.calls)
    assert n > 1, "超巨型草稿必须分块审计（多次调用）"
    # A4.9 r1 C1 回归钉：全覆盖 accept 后不得落回超预算单调用——
    # 调用次数必须恰等于分块数（多一次 = 重复审计/flip-flop 源）。
    from app.services.agent_loop import _split_audit_chunks
    from app.services.context_compressor import estimate_text_tokens_rough as _est
    # 复算分块数（与实现同公式）：共享上下文占位后估算
    # 不断言精确块数（实现内部细节），只断言「无额外单调用」——
    # 分块模式下每次调用的 user 内容都带块标记。
    assert all("第 " in _audit_user_text(c) and "块" in _audit_user_text(c)
               for c in loop.auditor.calls), "存在无分块标记的调用 = 落回了单调用审计"
    assert state.audit_budget_unaudited is False, "全覆盖 accept 不得误置未审计标记"
    joined_all = "".join(_audit_user_text(c) for c in loop.auditor.calls)
    for i in range(30):
        assert f"第{i:02d}段论点" in joined_all, f"第{i}段从未进入任何审计调用——静默丢块=断章取义"
    for c in loop.auditor.calls:
        assert "中间省略" not in _audit_user_text(c)
    assert any("第 1/" in _audit_user_text(c) or "块" in _audit_user_text(c)
               for c in loop.auditor.calls), "分块调用必须标注块序号/总块数"


def test_chunked_merge_reject_wins():
    # 第一次调用（首块）返回 reject，其余 accept → 合并必须 reject
    results = ['{"ok": false, "verdict": "reject", "problem": "本块含编造引用"}'] + \
              ['{"ok": true, "problem": ""}'] * 30
    loop = make_loop(results)
    state = _make_state()
    paras = [f"第{i:02d}段 [{i % 12 + 1}]。" + "论据" * 400 for i in range(30)]
    draft = "\n\n".join(paras)
    keys, olds = _force_tiny_budget()
    try:
        verdict = _run_audit(loop, state, draft)
    finally:
        for k, v in olds.items():
            _restore_audit_cfg(k, v)
    assert verdict is not None, "任一块 reject 时不得静默合格"
    assert verdict.verdict == "reject"
    assert "编造引用" in (verdict.problem or "")


def test_chunked_block_failure_flagged_never_dropped():
    # 首块抛异常，其余 accept：覆盖缺口显式置位，剩余块仍全部审计
    results = [RuntimeError("provider timeout")] + \
              ['{"ok": true, "problem": ""}'] * 30
    loop = make_loop(results)
    state = _make_state()
    paras = [f"第{i:02d}段 [{i % 12 + 1}]。" + "论据" * 400 for i in range(30)]
    draft = "\n\n".join(paras)
    keys, olds = _force_tiny_budget()
    try:
        verdict = _run_audit(loop, state, draft)
    finally:
        for k, v in olds.items():
            _restore_audit_cfg(k, v)
    assert len(loop.auditor.calls) > 1, "块失败不得中断其余块审计"
    assert state.audit_chunk_coverage_incomplete is True, "块失败必须显式置位 coverage incomplete"
    assert verdict is None, "缺口置位后按 fail-open 契约放行（低置信底线）"
