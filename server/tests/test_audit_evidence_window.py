"""Audit evidence window for long tool-heavy turns (conv 5abef2bf, 2026-09-13).

生产事故：37 次工具调用的回合里，第 14 次调用（browser，80k 字符，含
verify.yml / 16 任务 harness / 92.5% 等全部细节）落在审计证据窗
`state.tool_results[-12:]` 之外 → 审计员看到「台账无任何证据」→ 5 稿连拒
（"属凭空编造"）→ deterministic fallback 落库警示稿。

Fix：L1 台账列出本轮全部工具结果；文本选择按 grounding 优先 + 同类别最近
优先（token 预算仍是护栏；超预算条目标「截断」，审计模板判定顺序规则 4
禁止对截断/未展示证据判 reject）。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.agent_loop import _build_audit_evidence  # noqa: E402
from app.services.agent_loop import (  # noqa: E402
    ToolCallResult,
    _build_scout_pack,
    _parse_scout_selection,
)


class _State:
    def __init__(self, tool_results):
        self.tool_results = tool_results
        self.messages = []


def _tr(i, name, result, error=False):
    return ToolCallResult(call_id=f"c{i}", name=name, arguments={}, result=result, error=error)


def test_early_grounding_result_visible_in_long_turn():
    """决定性 browser 证据在长回合前段（第 5/30 个）也必须进入 evidence_text。"""
    results = []
    for i in range(30):
        if i == 4:
            results.append(_tr(
                i, "browser",
                "README: verify.yml runs Ubuntu macOS Windows, Python 3.9/3.12; "
                "16-task eval harness 92.5%; DECISIVE_MARKER_VERIFY_YML"))
        else:
            results.append(_tr(i, "terminal", f"terminal output line {i} ok"))
    ledger, text, _full = asyncio.run(_build_audit_evidence(_State(results), budget=100000))
    assert "DECISIVE_MARKER_VERIFY_YML" in text, (
        "早期 grounding 证据未进入审计视野 —— 长回合证据窗仍在截断")
    assert "browser" in ledger


def test_all_turn_results_listed_in_ledger_even_over_budget():
    """预算不足时：本轮每个工具结果仍须在 L1 台账列出（标截断），
    审计员据此判 unverifiable 而非「证据不存在=编造」。"""
    results = [
        _tr(i, "browser", f"PAGE-{i} " + "x" * 2000) for i in range(20)
    ]
    ledger, text, _full = asyncio.run(_build_audit_evidence(_State(results), budget=50))
    # 每条结果都有一行 [N] browser — 20 行（旧窗只列 12 行）
    assert ledger.count("browser") == 20, (
        f"台账未列全本轮工具结果：{ledger.count('browser')}/20")
    assert "截断" in ledger


def test_claim_relevant_grounding_prioritized_under_budget():
    """预算只够一条时：与草稿声称重叠的证据优先进入（两条单独可容、合计超限，
    展示序会先放旧条目 → 旧实现必失败，具鉴别力）。"""
    old = _tr(0, "browser", "OLD_UNRELATED_MARKER ok")
    new = _tr(1, "browser", "NEW_EVIDENCE_MARKER verify.yml")
    ledger, text, _full = asyncio.run(_build_audit_evidence(
        _State([old, new]), budget=8, draft_text="我认为 verify.yml 覆盖三个系统"))
    assert "NEW_EVIDENCE_MARKER" in text
    assert "OLD_UNRELATED_MARKER" not in text
    assert "截断" in ledger


def test_history_never_outranks_this_turn_on_tie():
    """零重叠平局时：本轮证据优先于历史轮次（recency 契约）。

    注意 messages[:-1] 会排除最后一条——必须追加一条尾部 assistant 消息，
    否则历史 tool 消息根本不会进入候选（A4.9 R2 实测空转）。
    """
    state = _State([_tr(0, "terminal", "THIS_TURN_MARKER ok")])
    state.messages = [
        {"role": "tool", "name": "terminal",
         "content": "HISTORY_MARKER ok", "tool_call_id": "h1"},
        {"role": "assistant", "content": "尾部消息（被 [:-1] 排除）"},
    ]
    ledger, text, _full = asyncio.run(_build_audit_evidence(
        state, budget=5, draft_text="完全不相关的草稿"))
    assert "THIS_TURN_MARKER" in text
    assert "HISTORY_MARKER" not in text
    assert "(历史轮次)" in ledger


def test_no_draft_falls_back_to_display_order():
    """无草稿文本时按展示序分配（向后兼容旧行为）。"""
    first = _tr(0, "browser", "FIRST_MARKER ok")
    second = _tr(1, "browser", "SECOND_MARKER ok")
    ledger, text, _full = asyncio.run(_build_audit_evidence(_State([first, second]), budget=5))
    assert "FIRST_MARKER" in text
    assert "SECOND_MARKER" not in text


# ---------------- 证据侦察（scout）纯函数与注入 ----------------

def test_parse_scout_selection_tolerates_fence_and_bad_values():
    ids, kws = _parse_scout_selection(
        '```json\n{"ids": [2, "5", 0, "x"], "keywords": ["verify.yml", ""]}\n```')
    assert ids == [2, 5]
    assert kws == ["verify.yml"]


def test_parse_scout_selection_bad_json():
    assert _parse_scout_selection("not json at all") == ([], [])
    assert _parse_scout_selection("") == ([], [])


def test_build_scout_pack_full_text_when_fits():
    trs = [_tr(0, "browser", "PACK_MARKER content")]
    pack = asyncio.run(_build_scout_pack(trs, [1], []))
    assert "PACK_MARKER" in pack


def test_build_scout_pack_keyword_window_when_over_budget():
    big = "x" * 100000 + "KEY_NEEDLE" + "y" * 100000
    trs = [_tr(0, "browser", big)]
    pack = asyncio.run(_build_scout_pack(trs, [1], ["KEY_NEEDLE"], budget_tokens=400))
    assert "KEY_NEEDLE" in pack
    assert len(pack) < 5000


def test_scout_method_sets_pack_once(monkeypatch):
    import app.services.auxiliary_client as aux_mod

    class _FakeAux:
        def __init__(self, task="default", llm=None):
            pass

        async def complete(self, messages, **kw):
            return '{"ids": [1], "keywords": ["MARK"]}'

    monkeypatch.setattr(aux_mod, "AuxiliaryClient", _FakeAux)
    from app.services.agent_loop import AgentLoop
    loop = AgentLoop.__new__(AgentLoop)
    state = _State([_tr(0, "browser", "MARK evidence text")])
    state.scout_used = False
    state.scout_pack = None
    asyncio.run(loop._scout_missing_evidence(state, "draft", "problem"))
    assert state.scout_used is True
    assert "MARK evidence text" in (state.scout_pack or "")


def test_scout_method_bad_json_noop(monkeypatch):
    import app.services.auxiliary_client as aux_mod

    class _FakeAux:
        def __init__(self, task="default", llm=None):
            pass

        async def complete(self, messages, **kw):
            return "not json at all"

    monkeypatch.setattr(aux_mod, "AuxiliaryClient", _FakeAux)
    from app.services.agent_loop import AgentLoop
    loop = AgentLoop.__new__(AgentLoop)
    state = _State([_tr(0, "browser", "some evidence")])
    state.scout_used = False
    state.scout_pack = None
    asyncio.run(loop._scout_missing_evidence(state, "draft", "problem"))
    assert state.scout_used is True
    assert state.scout_pack is None


def test_scout_pack_included_in_audit_evidence():
    state = _State([_tr(0, "terminal", "regular output")])
    state.scout_pack = "SCOUT_PACK_MARKER evidence slice"
    ledger, text, _full = asyncio.run(_build_audit_evidence(state, budget=100000))
    assert "SCOUT_PACK_MARKER" in text
    assert "[scout]" in ledger


def test_scout_pack_fragment_label_when_over_budget():
    """A4.9 R2 Minor：包超预算被片段化时台账须标「截断」而非「完整」。"""
    state = _State([_tr(0, "terminal", "x")])
    state.scout_pack = "PACK " + "y" * 10000
    ledger, text, _full = asyncio.run(_build_audit_evidence(state, budget=50))
    assert "[scout]" in ledger
    assert "截断" in ledger
    # 权威块（[S] 系统事实）也会带「— 完整」标注；本测试只钉 scout 行的标注
    _scout_line = next(ln for ln in ledger.splitlines() if "[scout]" in ln)
    assert "截断" in _scout_line
    assert "— 完整" not in _scout_line


def test_dedup_distinguishes_results_sharing_first_200_chars():
    """R2（2026-09-13 审计 F-H）：旧去重键 `(name, result[:200])` 会整条
    丢弃「前 200 字符相同」的不同结果（同页头/同 banner 的两个 browser
    输出）——证据静默消失。去重必须按全量内容判定：只有逐字节相同才算
    重复，同头不同尾两条都进台账。"""
    prefix = "SAME-HEADER " + "x" * 200
    a = _tr(0, "browser", prefix + " TAIL_ALPHA")
    b = _tr(1, "browser", prefix + " TAIL_BETA")
    ledger, text, _full = asyncio.run(_build_audit_evidence(_State([a, b]), budget=100000))
    assert ledger.count("browser") == 2, (
        f"同头结果被去重丢弃：台账仅 {ledger.count('browser')}/2 条")
    assert "TAIL_ALPHA" in text and "TAIL_BETA" in text


def test_over_budget_evidence_present_in_full_corpus():
    """R1（2026-09-13 审计 F-A/M1）：显示预算裁剪只应影响 LLM 看到的
    display_text；确定性闸门（NPG/claim gate/settled view）消费的全量语料
    必须覆盖超预算条目——否则回执锚点仍会因窗口裁剪而缺失（M1 不可满足
    循环）。第三条返回值 = 未受预算裁剪的确定性语料。"""
    big = _tr(0, "browser", "DECISIVE_OVER_BUDGET_MARKER " + "x" * 5000)
    small = _tr(1, "terminal", "small output ok")
    out = asyncio.run(_build_audit_evidence(_State([big, small]), budget=50))
    assert len(out) == 3, "缺少确定性全量语料第三返回值（full_text）"
    _ledger, text, full = out
    assert "DECISIVE_OVER_BUDGET_MARKER" not in text, "预算 50 下该证据不应出现在 display"
    assert "DECISIVE_OVER_BUDGET_MARKER" in full, "全量语料丢失超预算证据"
    assert "small output ok" in full


def test_full_corpus_superset_of_display():
    """不变量：display 中出现的每条证据都必须在 full 中（full 是超集）；
    full 额外覆盖被预算裁掉的条目。"""
    a = _tr(0, "browser", "ALPHA_MARKER " + "x" * 3000)
    b = _tr(1, "terminal", "BETA_MARKER ok")
    _ledger, text, full = asyncio.run(_build_audit_evidence(
        _State([a, b]), budget=30, draft_text="BETA_MARKER"))
    assert "BETA_MARKER" in text, "构造前提失败：display 应含预算内的 BETA 证据"
    assert "BETA_MARKER" in full, "display 有 BETA 而 full 缺失（超集不变量破坏）"
    assert "ALPHA_MARKER" in full, "超预算条目未进全量语料"


def test_over_budget_evidence_grounds_auditor_numeric_gate():
    """M1 行为锁（验收 #3）：审计断言值只存在于超预算工具结果时——full 语料
    使数值门保留 reject（断言值见于证据），display 语料则误降级 needs_evidence
    （旧行为）。证明确定性锚点确实来自全量域。"""
    from app.services.agent_loop import _apply_numeric_gate

    big = _tr(0, "terminal", "x" * 3000 + " 288768")
    _ledger, display, full = asyncio.run(_build_audit_evidence(_State([big]), budget=30))
    assert "288768" not in display, "构造前提失败：预算 30 下数字不应出现在 display"
    v_display, _p1 = _apply_numeric_gate("reject", "应为 288768", display)
    v_full, _p2 = _apply_numeric_gate("reject", "应为 288768", full)
    assert v_display == "needs_evidence", "display 缺证据时应降级（旧行为基线）"
    assert v_full == "reject", "full 有证据时应保留 reject（M1 修复语义）"


def test_full_corpus_cap_marks_truncation(monkeypatch):
    """残余①（R1 follow-up，A4.9 R1 修复版）：确定性全量语料默认无上限
    （M1 语义不变），可选上限 [agent.audit] full_evidence_max_tokens（0/负
    =不限）按**条目粒度 token 预算**执行（同一估算器），超限显式截断+无
    数字标注（标注进确定性域，注入数字会被误当锚点），绝不静默。"""
    from app.services.context_compressor import estimate_text_tokens_rough
    import app.services.agent_loop as al_mod

    big = _tr(0, "browser", "CAP_MARKER " + "x" * 5000)
    monkeypatch.setitem(al_mod.config.agent_audit, "full_evidence_max_tokens", 30)
    _ledger, _display, full = asyncio.run(_build_audit_evidence(_State([big]), budget=100000))
    assert "full-corpus truncated" in full, "配置上限后 full 语料未显式截断标注"
    _marker = full[full.index("[full-corpus truncated"):]
    assert "full_evidence_max_tokens" in _marker, "标注须给恢复指引"
    assert not any(ch.isdigit() for ch in _marker), (
        "标注不得含任何数字（标注进确定性域，数字会被误当证据锚点）")
    _evidence_part = full.split("\n\n[full-corpus truncated")[0]
    assert estimate_text_tokens_rough(_evidence_part) <= 30, "保留语料超过配置的 token 预算"
    assert "CAP_MARKER" in full, "首块超限时二分前缀应保留头部证据"

    # 默认 0 = 不限（不截断）
    monkeypatch.setitem(al_mod.config.agent_audit, "full_evidence_max_tokens", 0)
    _l2, _d2, full2 = asyncio.run(_build_audit_evidence(_State([big]), budget=100000))
    assert "full-corpus truncated" not in full2, "默认（0=不限）不得截断"
    assert len(full2) >= 5000

    # 负值同样视为不限（配置容错）
    monkeypatch.setitem(al_mod.config.agent_audit, "full_evidence_max_tokens", -1)
    _l3, _d3, full3 = asyncio.run(_build_audit_evidence(_State([big]), budget=100000))
    assert "full-corpus truncated" not in full3, "负值应视为不限"


def test_full_corpus_cap_keeps_whole_blocks(monkeypatch):
    """条目粒度：预算够两块则整块保留；只够第一块时第二块整体丢弃（不切块）。"""
    from app.services.context_compressor import estimate_text_tokens_rough
    import app.services.agent_loop as al_mod

    b1 = _tr(0, "terminal", "ONE_MARKER " + "a" * 200)
    b2 = _tr(1, "terminal", "TWO_MARKER " + "b" * 200)
    _l, _d, uncapped = asyncio.run(_build_audit_evidence(_State([b1, b2]), budget=100000))
    blocks = uncapped.split("\n\n")
    assert len(blocks) == 2
    t1 = estimate_text_tokens_rough(blocks[0])
    # 只够第一块 → 第二块整体丢弃
    monkeypatch.setitem(al_mod.config.agent_audit, "full_evidence_max_tokens", t1)
    _l2, _d2, capped = asyncio.run(_build_audit_evidence(_State([b1, b2]), budget=100000))
    assert "ONE_MARKER" in capped and "TWO_MARKER" not in capped
    assert "full-corpus truncated" in capped
    # 够两块 → 不截断（cap = 拼接后精确估算，含分隔符）
    monkeypatch.setitem(
        al_mod.config.agent_audit, "full_evidence_max_tokens",
        estimate_text_tokens_rough(uncapped))
    _l3, _d3, full3 = asyncio.run(_build_audit_evidence(_State([b1, b2]), budget=100000))
    assert "ONE_MARKER" in full3 and "TWO_MARKER" in full3
    assert "full-corpus truncated" not in full3

    # 多块 + "\n\n" 分隔符计入上限：joined 精确估算 ≤ cap（A4.9 R2 新 Minor 修复）
    many = [_tr(i, "terminal", f"block{i} " + "a" * 20) for i in range(10)]
    _l4, _d4, uncapped_many = asyncio.run(_build_audit_evidence(_State(many), budget=100000))
    _joined_est = estimate_text_tokens_rough(uncapped_many)
    monkeypatch.setitem(al_mod.config.agent_audit, "full_evidence_max_tokens", _joined_est - 1)
    _l5, _d5, capped_many = asyncio.run(_build_audit_evidence(_State(many), budget=100000))
    assert "full-corpus truncated" in capped_many
    _ev_many = capped_many.split("\n\n[full-corpus truncated")[0]
    assert estimate_text_tokens_rough(_ev_many) <= _joined_est - 1, (
        "多块拼接（含分隔符）后仍超上限")


def test_deterministic_gates_consume_full_corpus_not_display_budget():
    """验收 #3 接线锁：确定性消费者（NPG evaluate / numeric gate / claim
    grounding / settled view）必须消费 full 语料；LLM 提示词仍用预算后的
    display——防止未来回归到「机械闸门吃裁剪文本」（M1 复发面）。"""
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent / "app" / "services"
    text = "\n".join(
        (base / name).read_text(encoding="utf-8")
        for name in ("agent_loop.py", "agent_loop_audit.py", "agent_loop_orchestrator.py")
    )
    assert 'evidence_text=_evidence_full or ""' in text
    assert "_apply_numeric_gate(verdict, problem, _evidence_full)" in text
    assert "_apply_claim_grounding_gate(verdict, problem, _claims, _evidence_full)" in text
    assert text.count("_settled_items_view(state, evidence_text=_evidence_full)") >= 3
    assert "if _evidence_text:" in text, "LLM 提示词应继续使用预算后的 display 文本"
