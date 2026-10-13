"""RED tests (A4.8) — 审计质检结构性重构：advisory 判决处置策略（conv f831486f，2026-09-18）。

背景：20+ 波次补丁后，「多次修正仍未通过质检」警示语仍复发（生产累计 49 条）。
根因=LLM judge 拥有阻塞交付 + 驱动无界重写循环的权力，软判决语义不可枚举 →
每次误拒都要加确定性补丁；修复链（salvage→selection→确定性兜底）是对移动目标
的随机搜索（research: memory/eval_audit_architecture_20260918.md）。

修复契约：
  - `[agent.audit] policy` ∈ {"advisory"（默认）, "legacy"}；
  - advisory：LLM 软判决（unverifiable / needs_evidence, source=llm）只作建议，
    不重生成、不消耗预算、不进入 salvage/selection/警示语；出货静默（2026-09-18
    用户指令：conv 8b382ad8 生产再现「自动二次复核」说明，用户要求零残留）；
  - advisory：确定性可行动判决（source ∈ {npg, citation}）与硬 reject 至多
    一次定向修复（state.audit_repaired），修复后仍有判决 → 出货（矛盾/硬拒
    红线说明保留，通用说明不再追加）；
  - legacy：旧行为原样保留（一键回滚）。

Run: cd backend && python -m pytest tests/test_audit_advisory_policy.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import get_config
from app.services.agent_loop import (  # noqa: E402
    AgentLoop,
    AgentLoopState,
    AuditVerdict,
    _AUDIT_CONTRADICTION_EXCISED_NOTE,
    _AUDIT_CONTRADICTION_UNRESOLVED_NOTE,
    _AUDIT_UNRESOLVED_REJECT_NOTE,
    _advisory_audit_action,
    _contradiction_targets,
    _excise_claim_sentences,
    _is_contradiction_verdict,
)

config = get_config()
_AGENT_LOOP_SRC = open(
    os.path.join(os.path.dirname(__file__), "..", "app", "services", "agent_loop.py"),
    encoding="utf-8",
).read()


# ── Task 1: 配置属性 ─────────────────────────────────────────────────────────

def test_policy_default_is_advisory(monkeypatch):
    cfg = get_config()
    monkeypatch.setitem(cfg._config, "agent", {"audit": {}})
    assert cfg.agent_audit_policy == "advisory"


def test_policy_legacy_honored(monkeypatch):
    cfg = get_config()
    monkeypatch.setitem(cfg._config, "agent", {"audit": {"policy": "legacy"}})
    assert cfg.agent_audit_policy == "legacy"


def test_policy_invalid_falls_back_to_advisory(monkeypatch):
    cfg = get_config()
    monkeypatch.setitem(cfg._config, "agent", {"audit": {"policy": "banana"}})
    assert cfg.agent_audit_policy == "advisory"


def test_policy_case_insensitive(monkeypatch):
    cfg = get_config()
    monkeypatch.setitem(cfg._config, "agent", {"audit": {"policy": " Advisory "}})
    assert cfg.agent_audit_policy == "advisory"


# ── Task 2: 判决动作矩阵（纯函数） ─────────────────────────────────────────

def test_action_hard_reject_repairs_once():
    assert _advisory_audit_action("reject", "llm", False) == "repair"
    assert _advisory_audit_action("reject", "claim_verify", False) == "repair"


def test_action_deterministic_sources_repair_once():
    assert _advisory_audit_action("needs_evidence", "npg", False) == "repair"
    assert _advisory_audit_action("needs_evidence", "citation", False) == "repair"


def test_action_llm_soft_verdicts_ship():
    assert _advisory_audit_action("unverifiable", "llm", False) == "ship"
    assert _advisory_audit_action("needs_evidence", "llm", False) == "ship"


def test_action_contradiction_marked_soft_verdict_repairs_once():
    """软档但点名矛盾的判决也要拿到一次定向修复（生产 replay3 实况）。"""
    assert _advisory_audit_action(
        "needs_evidence", "llm", False, "数值与台账 5.53 bpw 矛盾，请重算") == "repair"
    assert _advisory_audit_action(
        "unverifiable", "llm", False, "与可见证据不一致") == "repair"
    # 已修复 → 一律出货
    assert _advisory_audit_action(
        "needs_evidence", "llm", True, "矛盾") == "ship"
    # 无矛盾标记的软判决仍只作建议
    assert _advisory_audit_action(
        "unverifiable", "llm", False, "证据被截断无法核对") == "ship"


def test_action_repaired_budget_exhausted_ships():
    assert _advisory_audit_action("reject", "llm", True) == "ship"
    assert _advisory_audit_action("needs_evidence", "npg", True) == "ship"


# ── Task 3: 主循环接线（静态回归，沿用项目既有弱回归锁先例） ────────────────

def test_state_has_one_shot_repair_flag():
    assert "audit_repaired" in AgentLoopState.__dataclass_fields__


def test_main_loop_reads_policy():
    assert "config.agent_audit_policy" in _AGENT_LOOP_SRC


def test_main_loop_advisory_branch_exists():
    # 定义 1 处 + draft/synthesis 各 1 处调用
    assert _AGENT_LOOP_SRC.count("_advisory_audit_gate(") >= 3, (
        "期望 AgentLoop._advisory_audit_gate 定义 1 + 调用 2"
    )
    # 动作矩阵函数：定义 1 处 + 方法体内 1 处调用
    assert _AGENT_LOOP_SRC.count("_advisory_audit_action(") >= 2
    assert "audit_metric outcome=advisory_ship" in _AGENT_LOOP_SRC


def test_advisory_generic_ship_note_removed():
    """用户指令（conv 8b382ad8 生产）：软判决出货不得再出现「自动二次复核」类
    用户可见说明——通用 advisory 说明常量与其消费点必须消失（回归锁）。"""
    assert "_AUDIT_ADVISORY_SHIP_NOTE" not in _AGENT_LOOP_SRC
    assert "未能完成自动二次复核" not in _AGENT_LOOP_SRC


# ── Task 3b: 执行级行为（A4.9 R1 Important-3：修复一次→出货的不变式） ────────

def _fake_loop():
    return AgentLoop(llm=object(), tool_schemas=[])


def _g(verdict, source, problem="问题", guidance="修正指令"):
    return AuditVerdict(verdict=verdict, source=source, problem=problem, guidance=guidance)


def test_exec_repair_once_then_ship():
    loop = _fake_loop()
    state = AgentLoopState()
    action = loop._advisory_audit_gate(
        state, _g("reject", "llm"), "draft-1", "rc-1", source="draft")
    assert action == "repair"
    assert state.audit_repaired is True
    assert state.audit_rejected_drafts[-1]["content"] == "draft-1"
    assert any(m.get("_rejected") for m in state.messages)
    assert any(m.get("_ephemeral") == "response_audit" for m in state.messages)
    _n = len(state.messages)
    # 第二次判决：修复预算已用 → 一律出货，不再改动上下文
    action2 = loop._advisory_audit_gate(
        state, _g("reject", "llm"), "draft-2", "rc-2", source="draft")
    assert action2 == "ship"
    assert len(state.messages) == _n
    assert state.audit_advisory_shipped is True


def test_exec_soft_verdict_ships_without_regeneration():
    """LLM 软判决只作建议：不改上下文、不产 stash、直接静默出货。"""
    loop = _fake_loop()
    state = AgentLoopState()
    _n = len(state.messages)
    action = loop._advisory_audit_gate(
        state, _g("needs_evidence", "llm"), "draft", "", source="draft")
    assert action == "ship"
    assert len(state.messages) == _n
    assert state.audit_rejected_drafts == []
    assert state.audit_advisory_shipped is True


def test_exec_contradiction_marked_soft_verdict_repairs_once_then_ships():
    """点名矛盾的软判决 → 一次定向修复；第二次无论判词 → 出货。"""
    loop = _fake_loop()
    state = AgentLoopState()
    g1 = _g("needs_evidence", "llm", problem="数值与台账 5.53 bpw 矛盾")
    assert loop._advisory_audit_gate(state, g1, "draft-1", "", source="draft") == "repair"
    assert state.audit_repaired is True
    g2 = _g("needs_evidence", "llm", problem="数值与台账 5.53 bpw 矛盾")
    assert loop._advisory_audit_gate(state, g2, "draft-2", "", source="draft") == "ship"
    assert state.audit_advisory_shipped is True


def test_exec_npg_repairs_once(monkeypatch):
    loop = _fake_loop()
    state = AgentLoopState()
    action = loop._advisory_audit_gate(
        state, _g("needs_evidence", "npg"), "draft", "", source="draft")
    assert action == "repair"
    assert state.audit_advisory_shipped is False


def test_exec_soft_note_removed_contradiction_notes_kept(monkeypatch):
    """用户指令：通用软判决说明彻底移除（开关开/关均静默）；矛盾/硬拒说明不受影响。"""
    cfg = get_config()
    cfg._config.setdefault("agent", {}).setdefault("audit", {})
    monkeypatch.setitem(cfg._config["agent"]["audit"], "low_confidence_note_enabled", True)
    loop = _fake_loop()
    state = AgentLoopState()
    g = _g("unverifiable", "llm")
    assert loop._advisory_audit_gate(state, g, "draft", "", source="draft") == "ship"
    assert state.audit_advisory_shipped is True, "结算标记与开关解耦"
    events, text = loop._advisory_ship_events(state, g, "原文")
    assert events == [] and text == "原文", "通用说明已移除（开关开也静默）"
    # 矛盾类不受开关管辖（红-line 不得被静默关闭）
    state2 = AgentLoopState()
    state2.audit_repaired = True  # 模拟「一次修复后仍判矛盾」的二次判决
    g2 = AuditVerdict(
        verdict="needs_evidence", source="llm", problem="矛盾", guidance="g",
        unsupported_claims=[{"claim": "草稿称 71.7 tok/s", "evidence_status": "contradicted"}],
    )
    assert loop._advisory_audit_gate(state2, g2, "draft", "", source="draft") == "ship"
    base = "命中 71.7 的句子。\n保留的足够长的正常内容，确保占比低于阈值。"
    events2, text2 = loop._advisory_ship_events(state2, g2, base)
    assert any("audit_reset" in e for e in events2)
    assert any(_AUDIT_CONTRADICTION_EXCISED_NOTE == e.get("content") for e in events2)


def test_advisory_branch_never_stashes_to_selection_caveat():
    """advisory 不得新增兜底链调用点：salvage 仅 legacy 两处（draft/synthesis），
    selection 仅 salvage 内部一处；警示语只在 legacy selection 出口（定义+注释+
    两处 yield = 4 次文本出现）。advisory 路径经 action 矩阵出货。"""
    assert _AGENT_LOOP_SRC.count("self._salvage_after_audit_budget(") == 2
    assert _AGENT_LOOP_SRC.count("self._select_best_rejected_draft(") == 1
    assert _AGENT_LOOP_SRC.count("_AUDIT_SELECTION_CAVEAT") == 4


def test_advisory_ship_flag_setter_present():
    """advisory 标记 setter 单点存在（唯一的置位点在 _advisory_audit_gate）。"""
    assert _AGENT_LOOP_SRC.count("state.audit_advisory_shipped = True") == 1
    assert "state.audit_advisory_shipped = False" in _AGENT_LOOP_SRC


# ── 第二波：矛盾结算（contradiction settlement，确定性切除 / 具体冲突说明）────

def test_is_contradiction_verdict_matrix():
    assert _is_contradiction_verdict(
        AuditVerdict(verdict="needs_evidence", source="llm", problem="数值与台账矛盾")) is True
    assert _is_contradiction_verdict(
        AuditVerdict(verdict="reject", source="llm", problem="与证据不一致")) is True
    assert _is_contradiction_verdict(
        AuditVerdict(verdict="reject", source="claim_verify", problem="分段核对 fail")) is True
    assert _is_contradiction_verdict(
        AuditVerdict(verdict="needs_evidence", source="llm", problem="请先调用 calculate 重算")) is False
    assert _is_contradiction_verdict(
        AuditVerdict(verdict="needs_evidence", source="npg", problem="推导数值无回执")) is False
    assert _is_contradiction_verdict(None) is False


def test_contradiction_targets_structured_preferred():
    g = AuditVerdict(
        verdict="reject", source="llm", problem="矛盾",
        unsupported_claims=[
            {"claim": "A 为 71.7 tok/s", "evidence_status": "contradicted"},
            {"claim": "B 为 5.53 bpw", "evidence_status": "missing"},
        ],
        contradicted_claims=["C 为 38.8 s"],
    )
    targets = _contradiction_targets(g)
    assert any("71.7" in t for t in targets)
    assert any("38.8" in t for t in targets)
    assert not any("5.53" in t for t in targets), "missing 状态不得进入切除目标"


def test_contradiction_targets_structured_only():
    """A4.9 I3：无结构化被驳声称时不得用判词全文 token（防误切正确值）。"""
    g = AuditVerdict(
        verdict="needs_evidence", source="llm",
        problem="草稿称 71.7，台账为 51.19，矛盾",
    )
    assert _contradiction_targets(g) == []
    g2 = AuditVerdict(
        verdict="needs_evidence", source="llm", problem="矛盾",
        unsupported_claims=[{"claim": "草稿称 71.7 tok/s", "evidence_status": "contradicted"}],
    )
    targets = _contradiction_targets(g2)
    assert any("71.7" in t for t in targets)


def test_excise_removes_matching_sentence_keeps_rest():
    text = (
        "第一句 71.7 tok/s 与台账不符。"
        "第二句正常内容保留，这一段是足够长的正常阐述，用于确保被删除内容的占比低于阈值。"
        "第三句 38.8 秒也矛盾。"
    )
    out, removed, matched = _excise_claim_sentences(text, [{"71.7"}, {"38.8"}])
    assert "71.7" not in out
    assert "38.8" not in out
    assert "第二句正常内容保留" in out
    assert removed == 2 and matched == 2


def test_excise_keeps_delimiters_between_kept_segments():
    text = "问题句 71.7。\n\n保留段落一。\n\n保留段落二。"
    out, removed, _ = _excise_claim_sentences(text, [{"71.7"}])
    assert removed == 1
    assert "保留段落一。\n\n保留段落二" in out, "未删句之间的原始分隔符（空行）必须保留"


def test_excise_fail_closed_when_removal_too_large():
    text = "71.7 矛盾一。71.7 矛盾二。正常。"
    out, removed, _ = _excise_claim_sentences(text, [{"71.7"}], max_ratio=0.5)
    assert out == text and removed == 0


def test_excise_no_match_returns_original():
    text = "全部是正常内容。"
    out, removed, _ = _excise_claim_sentences(text, [{"71.7"}])
    assert out == text and removed == 0


def test_excise_skips_generic_only_targets():
    """无特异性 token（无数字且 <4 字符）的目标不可定位 → 不切除。"""
    text = "含 tok 的句子。"
    out, removed, _ = _excise_claim_sentences(text, [{"tok"}])
    assert out == text and removed == 0


def test_excise_skips_structurally_sensitive_sentences():
    """A4.9 Minor-5：列表/表格/标题/围栏句不切除（防悬空序号/表格破坏）。"""
    text = "1. 命中 71.7 的列表项。\n| a | 71.7 |\n## 71.7 标题\n```\n71.7 code\n```\n正常句保留。71.7 矛盾句。"
    out, removed, _ = _excise_claim_sentences(text, [{"71.7"}])
    assert removed == 1
    assert "1. 命中 71.7 的列表项。" in out, "列表项不得被切"
    assert "| a | 71.7 |" in out, "表格行不得被切"
    assert "## 71.7 标题" in out, "标题不得被切"
    assert "```" in out, "围栏不得被切"
    assert "正常句保留。" in out
    assert "71.7 矛盾句。" not in out


def test_settle_excises_and_emits_specific_note():
    loop = _fake_loop()
    g = AuditVerdict(
        verdict="needs_evidence", source="llm",
        problem="与台账矛盾", guidance="g",
        unsupported_claims=[
            {"claim": "草稿称 71.7 tok/s", "evidence_status": "contradicted"},
        ],
    )
    text = "这句 71.7 是错的。\n这句保留，其余内容足够长以确保未被删除部分占比超过一半。"
    settled, note = loop._settle_advisory_ship(g, text)
    assert "71.7" not in settled and "这句保留" in settled
    assert note == _AUDIT_CONTRADICTION_EXCISED_NOTE


def test_settle_unresolved_keeps_original_with_conflict_note():
    loop = _fake_loop()
    g = AuditVerdict(
        verdict="needs_evidence", source="llm",
        problem="与来源存在矛盾", guidance="g",  # 无结构化被驳声称
    )
    text = "正常内容一。正常内容二。"
    settled, note = loop._settle_advisory_ship(g, text)
    assert settled == text
    assert note == _AUDIT_CONTRADICTION_UNRESOLVED_NOTE


def test_settle_unstructured_problem_tokens_do_not_excise():
    """A4.9 I3：判词里出现正确值（台账 51.19）也不得被当作切除目标。"""
    loop = _fake_loop()
    g = AuditVerdict(
        verdict="needs_evidence", source="llm",
        problem="草稿称 71.7，台账为 51.19，矛盾", guidance="g",
    )
    text = "正确句：台账值为 51.19。错误句：草稿值为 71.7。"
    settled, note = loop._settle_advisory_ship(g, text)
    assert settled == text, "无结构化声称时不得切除（正确值共存）"
    assert note == _AUDIT_CONTRADICTION_UNRESOLVED_NOTE


def test_settle_second_hard_reject_gets_unresolved_reject_note():
    """A4.9 I2：二次硬拒（非矛盾族）→ 具体未通过说明，不冒充矛盾语义。"""
    loop = _fake_loop()
    g = AuditVerdict(
        verdict="reject", source="llm", problem="答非所问，未回答最新问题", guidance="g",
    )
    text = "正常内容。"
    settled, note = loop._settle_advisory_ship(g, text)
    assert settled == text
    assert note == _AUDIT_UNRESOLVED_REJECT_NOTE


def test_settle_non_contradiction_ships_silently():
    """用户指令（conv 8b382ad8）：非矛盾软判决出货零说明。"""
    loop = _fake_loop()
    g = AuditVerdict(
        verdict="needs_evidence", source="llm",
        problem="请先调用 calculate 实际重算", guidance="g",
    )
    text = "正常内容。"
    settled, note = loop._settle_advisory_ship(g, text)
    assert settled == text
    assert note == ""


def test_ship_events_soft_verdict_zero_events():
    """软判决结算：文本不变、零事件（静默出货）。"""
    loop = _fake_loop()
    state = AgentLoopState()
    state.audit_advisory_shipped = True
    g = AuditVerdict(verdict="needs_evidence", source="llm", problem="无法核实", guidance="g")
    events, text = loop._advisory_ship_events(state, g, "原文")
    assert events == [] and text == "原文"
    assert state.audit_advisory_shipped is False


def test_ship_events_excise_reset_then_note():
    """A4.9 I1 修复后的出货点契约：audit_reset → 结算稿 → 说明，顺序固定。"""
    loop = _fake_loop()
    state = AgentLoopState()
    state.audit_advisory_shipped = True
    g = AuditVerdict(
        verdict="needs_evidence", source="llm", problem="矛盾", guidance="g",
        unsupported_claims=[
            {"claim": "草稿称 71.7 tok/s", "evidence_status": "contradicted"},
        ],
    )
    base = "命中 71.7 的句子。\n保留的足够长的正常内容，确保占比低于阈值。"
    events, text = loop._advisory_ship_events(state, g, base)
    assert state.audit_advisory_shipped is False
    assert events[0] == {"audit_reset": True}
    assert events[1]["content"] == text and "71.7" not in text
    assert events[-1]["content"] == _AUDIT_CONTRADICTION_EXCISED_NOTE


def test_ship_events_no_flag_is_noop():
    loop = _fake_loop()
    state = AgentLoopState()
    events, text = loop._advisory_ship_events(
        state, AuditVerdict(verdict="needs_evidence", source="llm"), "原文")
    assert events == [] and text == "原文"


def test_ship_points_call_settlement():
    """两个出货点（draft/synthesis）都必须经过结算事件方法（定义 1 + 调用 2）。"""
    assert _AGENT_LOOP_SRC.count("_advisory_ship_events(") >= 3


def test_shipped_text_wiring_locked():
    """A4.9 C1 回归锁：出货结算基于被审计文本（拼接审计=完整拼接稿），
    防止未来改回 assistant_content 造成丢头。"""
    assert "_shipped_text = _audit_target" in _AGENT_LOOP_SRC
    assert "_shipped_text if _shipped_text is not None" in _AGENT_LOOP_SRC
