"""M5 层 3（R4）— remand 映射简表 + 「未被采用」矛盾声明否决。

plan_citation_full_discipline_ref_20261011 M5：
- remand 指导语附 id|title 三元组简表（范围 → 映射）；
- 「本轮检索结果未被采用」声明与台账内容矛盾时审计否决该声明
  （conv 68423d97：276 条台账在库仍以「未被采用」脱身，实为使用后不标）。

Run: cd backend && python -m pytest tests/test_citation_declaration_veto.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.agent_loop import AgentLoopState, _citation_remand_verdict
from app.services.citation_ledger import CitationLedger

USER_MSG = "请考证摄山志的编纂与版本。"

SNIPPET = "摄山志卷四记载乾隆皇帝南巡驻跸栖霞行宫并题诗三十余首"


def _make_state() -> AgentLoopState:
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    ledger = CitationLedger()
    ledger.register(
        "https://shidianguji.com/book/SDZ123",
        title="摄山志:八卷", snippet=SNIPPET,
        author="陈毅", edition="刻本", year="1790", source_type="book",
    )
    ledger.register("https://example.com/other", title="无关来源", snippet="与摄山志无关的摘要")
    state.citation_ledger = ledger
    return state


def _draft(body: str) -> str:
    # ≥ 200 字的实质性草稿（remand_min_chars 阈值）
    return body + "。" + "考证内容" * 60


def test_remand_guidance_includes_mapping_table():
    state = _make_state()
    verdict = _citation_remand_verdict(state, _draft("本文讨论摄山志的版本源流"))
    assert verdict is not None
    assert verdict.verdict == "needs_evidence"
    # id|title 简表在指导语中
    assert "[1] 摄山志" in verdict.guidance
    assert "[2] 无关来源" in verdict.guidance
    assert "陈毅" in verdict.guidance


def test_declaration_with_overlap_vetoed():
    state = _make_state()
    draft = _draft("本轮检索结果未被采用。需要说明的是，" + SNIPPET[:16] + "，可见其地位特殊")
    verdict = _citation_remand_verdict(state, draft)
    assert verdict is not None, "矛盾声明必须被否决（继续 remand 要求补标）"
    assert "矛盾" in verdict.problem or "不成立" in verdict.problem
    assert "未被采用" in verdict.problem


def test_declaration_without_overlap_honored():
    state = _make_state()
    draft = _draft("本轮检索结果未被采用。以下内容基于此前对话与常识：地理环境决定论概述")
    verdict = _citation_remand_verdict(state, draft)
    assert verdict is None, "声明成立（无实质重合）时应尊重逃生口"


def test_no_declaration_zero_citations_still_remand():
    state = _make_state()
    verdict = _citation_remand_verdict(state, _draft("本文考证版本源流而未标注编号"))
    assert verdict is not None
    assert verdict.source == "citation"


def test_generic_ascii_phrase_no_false_veto():
    """C3（遗留清零）：通用短英文短语（<40 字符窗）不得构成矛盾证据。"""
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    ledger = CitationLedger()
    ledger.register(
        "https://example.com/study",
        title="A Study",
        snippet="Results were computed based on the results of the study X47",
    )
    state.citation_ledger = ledger
    draft = _draft("本轮检索结果未被采用。The analysis was based on the results of the prior pipeline")
    verdict = _citation_remand_verdict(state, draft)
    assert verdict is None, "通用短语误报：必须尊重声明（无实质重合）"


def test_with_valid_citation_no_remand():
    state = _make_state()
    verdict = _citation_remand_verdict(state, _draft("摄山志卷四有记载 [1]"))
    assert verdict is None


def test_declaration_with_ascii_overlap_vetoed():
    """A4.9 B9/⑪：英文标题/摘要的复用同样构成矛盾声明。"""
    state = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    ledger = CitationLedger()
    ledger.register(
        "https://arxiv.org/abs/1706.03762",
        title="Attention Is All You Need",
        snippet="The dominant sequence transduction models are based on complex recurrent networks",
    )
    state.citation_ledger = ledger
    draft = _draft(
        "本轮检索结果未被采用。The dominant sequence transduction models are based on "
        "complex recurrent networks, so we propose a new architecture"
    )
    verdict = _citation_remand_verdict(state, draft)
    assert verdict is not None, "英文重合的「未被采用」声明必须被否决"
    assert "矛盾" in verdict.problem or "不成立" in verdict.problem
    # A-F3：仅复制**短标题**（四词、25 字符）也必须可判——标题探针下限 16
    state2 = AgentLoopState(messages=[{"role": "user", "content": USER_MSG}])
    led2 = CitationLedger()
    led2.register("https://arxiv.org/abs/1706.03762", title="Attention Is All You Need")
    state2.citation_ledger = led2
    draft2 = _draft("本轮检索结果未被采用。Interesting paper titled Attention Is All You Need")
    verdict2 = _citation_remand_verdict(state2, draft2)
    assert verdict2 is not None, "标题复用也必须否决（下限 16 生效）"
