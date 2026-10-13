"""conv 1a24a04a 末答零引用根治波（2026-09-25）— F1/F2/F3 行为测试。

事故链（60 生产实证）：
① NPG enforce 把「1683万/259万」误判「未出现在任何工具输出中」——数值来自
   **更早轮次** web_search，而历史证据窗（6 条）被更近轮次的 notes/memory/
   execute_code 占满 → grounding 类历史结果零入选 → 跨轮假阳性；
② 该假阳性消耗本轮唯一 repair（_advisory_audit_action repaired=True→ship）；
③ 模型按「只改两处、其余逐字保留」重出 5582 字时丢弃全部 ~30 个 [N]；
④ 零引用闸门（cited=0/ledger=87）必触发，但 repaired=True → 静默出货。

F1: 历史工具证据类别感知选择——grounding 类（web_search/browser/MCP）独立
    窗口，不被非 grounding 挤占。
F2: 零引用 remand（source="citation"，one-shot 自限）豁免唯一 repair 预算。
F3: 定点修正指导语显式要求保留全部 [N] 引用标记。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.agent_loop import (  # noqa: E402
    _AUDIT_GUIDANCE_LOCALIZED_CLAUSE,
    _advisory_audit_action,
    _build_audit_evidence,
    _citation_remand_verdict,
)
from app.services.citation_ledger import CitationLedger  # noqa: E402
from app.services.numeric_provenance_gate import evaluate_numeric_provenance  # noqa: E402


class _State:
    def __init__(self, tool_results=None, messages=None):
        self.tool_results = tool_results or []
        self.messages = messages or []


_SEARCH_SNIPPET = (
    "27. 智能体中标十大项目盘点\nURL: https://example.com/top10\n"
    "摘要: 新华保险多智能体系统项目成交金额 1683 万元（中电信数智中标）；"
    "国家管网 AI Agent 能力平台项目成交金额 259 万元（科大讯飞中标）。"
)


def _history_messages():
    """conv 1a24a04a 形态：较早轮次的 web_search 结果之后，更近轮次堆了
    ≥6 条非 grounding 工具消息（execute_code/notes/memory）。"""
    msgs = [{"role": "user", "content": "早轮问题"}, {"role": "assistant", "content": "早轮回答"}]
    msgs.append({"role": "tool", "name": "web_search", "tool_call_id": "h1", "content": _SEARCH_SNIPPET})
    for i in range(7):
        msgs.append({"role": "tool", "name": "notes", "tool_call_id": f"n{i}", "content": f"笔记操作结果 {i}"})
        msgs.append({"role": "tool", "name": "memory", "tool_call_id": f"m{i}", "content": f"记忆操作结果 {i}"})
    msgs.append({"role": "user", "content": "本轮问题"})
    return msgs


def test_history_grounding_window_not_starved_by_non_grounding(monkeypatch):
    """F1: grounding 类历史结果不得被非 grounding 历史消息挤出窗口——
    仅出现在历史轮 web_search 的数值必须进入确定性全量语料（NPG 溯源域）。
    A4.9 R1 Minor-3：密封化——显式钉住两窗默认值，不随部署配置漂移。"""
    from app.core.config import get_config
    _cfg = get_config()
    monkeypatch.setattr(type(_cfg), "agent_audit_history_grounding_max_items", property(lambda self: 20))
    monkeypatch.setattr(type(_cfg), "agent_audit_history_other_max_items", property(lambda self: 6))
    state = _State(messages=_history_messages())
    _ledger, _display, full = asyncio.run(_build_audit_evidence(state, budget=100000))
    assert "1683" in full and "259" in full, (
        "历史轮 web_search 结果被非 grounding 消息挤出证据窗 —— "
        "NPG 将对跨轮引用数值误报「未出现在任何工具输出中」(conv 1a24a04a)")
    assert "智能体中标十大" in full


def test_history_window_disclosure_names_class_windows():
    """F1 披露（A4.9 R1 Minor-2 收紧）：历史窗之外仍有结果时，台账必须按类别
    披露窗口与省略条数（未展示 ≠ 不存在）。"""
    msgs = _history_messages()
    for i in range(30):
        msgs.insert(2, {"role": "tool", "name": "web_search", "tool_call_id": f"g{i}",
                        "content": f"{i}. 历史检索条目 {i}\nURL: https://example.com/{i}\n摘要: 条目{i}"})
    state = _State(messages=msgs)
    ledger, _display, _full = asyncio.run(_build_audit_evidence(state, budget=100000))
    assert "历史窗" in ledger and "历史证据截断" in ledger
    assert "grounding 窗口" in ledger, "披露必须点名 grounding 窗口（类别分窗契约）"
    assert "另有" in ledger and "省略" in ledger, "必须披露省略条数"


def test_npg_cross_turn_quoted_numbers_not_flagged():
    """F1 纯函数契约：证据语料含历史检索数值时，跨轮引用数值不触发 NPG。"""
    draft = "新华保险多智能体系统项目成交金额 1683 万元，国家管网项目 259 万元。"
    flagged_report = evaluate_numeric_provenance(
        draft=draft, evidence_text="无关语料", user_messages=[], current_tool_calls=[])
    clean_report = evaluate_numeric_provenance(
        draft=draft, evidence_text=_SEARCH_SNIPPET, user_messages=[], current_tool_calls=[])
    assert not clean_report.flagged, (
        f"数值在证据语料中（历史轮检索结果）却被 NPG 标记: "
        f"{[f.raw for f in clean_report.flags]}")
    # 差分对照（§A4.10）：证据缺失时的行为按既有闸门语义记录，不强制 flagged
    # （纯知识点数值本就不在触发面）——本用例钉住的是「有证据→不标记」。
    _ = flagged_report


def test_advisory_citation_remand_exempt_after_repair():
    """F2: 零引用闸门（one-shot 自限）豁免唯一 repair 预算；其余 source 不变。"""
    assert _advisory_audit_action("needs_evidence", "citation", True) == "repair", (
        "repaired=True 时零引用 remand 被静默出货 —— conv 1a24a04a 出货链最后一环")
    assert _advisory_audit_action("needs_evidence", "npg", True) == "ship"
    assert _advisory_audit_action("needs_evidence", "llm", True) == "ship"
    assert _advisory_audit_action("unverifiable", "llm", True) == "ship"
    # 未消耗预算时各 source 行为不变
    assert _advisory_audit_action("needs_evidence", "citation", False) == "repair"
    assert _advisory_audit_action("needs_evidence", "npg", False) == "repair"
    assert _advisory_audit_action("needs_evidence", "llm", False) == "ship"
    assert _advisory_audit_action("reject", "llm", False) == "repair"
    assert _advisory_audit_action("reject", "llm", True) == "ship"


def test_zero_citation_remand_fires_and_repairs_after_npg_spent_budget():
    """F2 组合（事故形态回放）：NPG 已消耗 repair 后，通篇无 [N] 的检索轮
    草稿仍被 remand 打回（而非静默出货）。"""
    ledger = CitationLedger()
    for i in range(1, 4):
        ledger.register(f"https://example.com/s{i}", title=f"来源{i}", snippet=f"摘要{i}")
    state = _State()
    state.citation_ledger = ledger
    state.citation_remanded = False
    draft = "本轮答案。" + "详" * 300  # ≥ remand_min_chars，零 [N]
    verdict = _citation_remand_verdict(state, draft)
    assert verdict is not None and verdict.source == "citation"
    assert _advisory_audit_action(verdict.verdict, verdict.source, True) == "repair"


def test_localized_clause_preserves_citation_markers():
    """F3: 定点修正指导语显式要求保留全部 [N] 引用标记及其位置。"""
    assert "[N]" in _AUDIT_GUIDANCE_LOCALIZED_CLAUSE
    assert "引用标记" in _AUDIT_GUIDANCE_LOCALIZED_CLAUSE
    assert "严禁" in _AUDIT_GUIDANCE_LOCALIZED_CLAUSE
    # A4.9 R1 Minor-1：模型可见指导语不得夹带内部事故编号/元叙事
    assert "conv" not in _AUDIT_GUIDANCE_LOCALIZED_CLAUSE
    assert "生产实证" not in _AUDIT_GUIDANCE_LOCALIZED_CLAUSE


def test_citation_remand_rearms_once_per_turn():
    """A4.9 R1 Important-1：remand 打回后的修复稿若仍零 [N]，允许第二次 remand
    （每轮上限 2，有界不循环）；第三次起不再打回。"""
    ledger = CitationLedger()
    for i in range(1, 4):
        ledger.register(f"https://example.com/s{i}", title=f"来源{i}", snippet=f"摘要{i}")
    state = _State()
    state.citation_ledger = ledger
    state.citation_remanded = False
    draft = "本轮答案。" + "详" * 300
    v1 = _citation_remand_verdict(state, draft)
    assert v1 is not None, "首次 remand 必须触发"
    assert state.citation_remanded is True
    v2 = _citation_remand_verdict(state, draft)
    assert v2 is not None, "修复稿仍零引用时必须再 remand 一次（否则静默出货复发 conv 1a24a04a 形态）"
    v3 = _citation_remand_verdict(state, draft)
    assert v3 is None, "每轮 remand 上限 2 —— 第三次起放行（有界）"
    # 两次 remand 在 repaired=True 下都可 repair（F2 豁免覆盖两次）
    assert _advisory_audit_action(v2.verdict, v2.source, True) == "repair"
