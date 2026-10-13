"""TDD tests: 死磕「未执行占位」完成闸门 + 用户批评返工链路（conv a104fbc5，2026-09-20）。

事故：实证章节结果表全部是「受限估计：待执行/不填报数值」占位，judge 仅凭文件存在/结构判 done；
用户后续批评（"写得糟糕、全是占位"）被 search_demand 守卫改写成联网核实，回答"无需修改"。

修复面：
- A: 机械占位扫描 → judge 证据 + verifier 证据/规则 + criteria 起草规则（未执行占位不得判完成）
- B: search/tool 守卫指令锚定本轮用户消息（不得替代用户诉求）
- C: classify_intent 新增 REVISE（负面评价/返工指令不得落入 DISCUSS）+ begin_revision 重开执行

本文件按 A4.8 先行：实现前运行必须 RED。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_deathmatch_placeholder_gate.py -v
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.deathmatch_service as ds
from app.services.deathmatch_service import DeathmatchManager


# ── fixtures ──────────────────────────────────────────────────────────────

def _conv(**over):
    base = dict(
        id="conv-placeholder",
        deathmatch_mode=True,
        deathmatch_status="active",
        deathmatch_goal="测试目标：产出一份报告",
        deathmatch_turns=0,
        deathmatch_max_turns=0,
        deathmatch_verify_failures=0,
        deathmatch_consecutive_failures=0,
        deathmatch_verdict=None,
        deathmatch_reason=None,
        deathmatch_plan={"steps": [
            {"id": "s1", "status": "in_progress", "description": "步骤1",
             "expected_output": "a.txt", "verification_method": "", "boundary": ""},
        ]},
        deathmatch_plan_version=1,
        deathmatch_reflections=[],
        deathmatch_last_verification_result=None,
        deathmatch_wall_time_started_at=None,
        deathmatch_max_wall_time_seconds=0,
        deathmatch_wall_time_used_seconds=0,
        deathmatch_grilling_qa_history=[],
        deathmatch_grilling_complete=True,
        deathmatch_human_gate=None,
        deathmatch_bible_draft=None,
        deathmatch_settled_ledger=None,
        deathmatch_subgoals=[],
        deathmatch_acceptance_criteria=None,
        deathmatch_failed_directions=None,
        deathmatch_pause_state=None,
        deathmatch_events=None,
        deathmatch_no_progress_replans=0,
        deathmatch_context_summary=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _creative_cache():
    key = ds._normalize_goal_key("测试目标：产出一份报告")
    ds._CREATIVE_GOAL_CACHE[key] = False
    yield
    ds._CREATIVE_GOAL_CACHE.pop(key, None)


def _run(coro):
    return asyncio.run(coro)


_PLACEHOLDER_DOC = (
    "# 第6章 实证分析（初稿）\n\n"
    "本章遵守证据纪律：未完成估计的部分标注受限估计。\n\n"
    "| 命题 | 执行状态 |\n| --- | --- |\n"
    "| P1 传导阻滞 | 受限估计：不填报数值 |\n"
    "| P2 配置偏向 | 受限估计：不填报数值 |\n"
    "| 事件研究 | 不可识别/受限估计：待执行 |\n"
    "| 稳健性 | 受限估计：待执行 |\n"
)

_CLEAN_DOC = "# 报告\n\n2025 年 GDP 增长 5.0%，数据来源国家统计局。\n"


# ── A1（r7）：交付物全文有界摘录（agentic 判定输入；无关键词表） ─────────

def test_excerpt_reads_declared_deliverables_full_text(tmp_path):
    ws = str(tmp_path)
    (tmp_path / "empirical.md").write_text(_PLACEHOLDER_DOC, encoding="utf-8")
    (tmp_path / "clean.md").write_text(_CLEAN_DOC, encoding="utf-8")
    ev = ds._read_deliverable_excerpt(ws, ["empirical.md", "clean.md"])
    assert "交付物内容摘录" in ev
    assert "受限估计：不填报数值" in ev, "表格正文必须完整可见（非仅首尾）"
    assert "2025 年 GDP 增长 5.0%" in ev


def test_excerpt_empty_for_missing_and_binary_files(tmp_path):
    ws = str(tmp_path)
    (tmp_path / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    assert ds._read_deliverable_excerpt(ws, ["nope.md", "img.png"]) == ""


def test_excerpt_deduplicates_files(tmp_path):
    ws = str(tmp_path)
    (tmp_path / "e.md").write_text(_PLACEHOLDER_DOC, encoding="utf-8")
    ev = ds._read_deliverable_excerpt(ws, ["e.md", "e.md", "e.md"])
    assert ev.count("--- e.md ---") == 1, "重复文件只登记一次"


def test_excerpt_discloses_file_cap(tmp_path):
    ws = str(tmp_path)
    for i in range(13):
        (tmp_path / f"f{i:02d}.md").write_text("content", encoding="utf-8")
    ev = ds._read_deliverable_excerpt(ws, [f"f{i:02d}.md" for i in range(13)])
    assert "未含在本摘录中" in ev, "文件上限必须显式披露"


def test_no_keyword_marker_tables_remain():
    """用户红线（r7）：占位判定必须 agentic——禁止重新引入关键词表/扫描器。"""
    for banned in ("_PLACEHOLDER_MARKERS", "_PLACEHOLDER_BLOCKING_MARKERS",
                   "_PLACEHOLDER_MARKER_EXCLUSIONS", "_PLACEHOLDER_EXCLUSION_GUARDS",
                   "_DEGRADE_ALLOW_HINTS", "_EMPIRICAL_HINTS"):
        assert not hasattr(ds, banned), f"不得重新引入关键词机制: {banned}"
    assert not hasattr(DeathmatchManager, "_scan_unexecuted_placeholders")


# ── A2: judge 证据注入（r7：全文摘录） ───────────────────────────────────

def test_judge_evidence_injects_deliverable_excerpt(tmp_path):
    (tmp_path / "paper.md").write_text(_PLACEHOLDER_DOC, encoding="utf-8")
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s8", "status": "done", "description": "合成论文",
         "output_files": ["paper.md"], "expected_output": "paper.md"},
    ]})
    mgr = DeathmatchManager(conv)
    ev = mgr._build_judge_evidence(str(tmp_path))
    assert "交付物内容摘录" in ev and "paper.md" in ev
    assert "受限估计：不填报数值" in ev, "交付物正文必须进入 judge 证据"


def test_judge_evidence_clean_workspace_has_no_placeholder_content(tmp_path):
    (tmp_path / "paper.md").write_text(_CLEAN_DOC, encoding="utf-8")
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s8", "status": "done", "description": "合成论文",
         "output_files": ["paper.md"], "expected_output": "paper.md"},
    ]})
    mgr = DeathmatchManager(conv)
    ev = mgr._build_judge_evidence(str(tmp_path))
    assert "受限估计" not in ev


def test_judge_evidence_keeps_excerpt_on_large_workspace(tmp_path):
    """证据包尾部截断时，交付物摘录必须保住（放在工作区清单之前）。"""
    for i in range(400):
        (tmp_path / f"f{i:03d}.md").write_text(("x" * 200) + "\n", encoding="utf-8")
    (tmp_path / "paper.md").write_text(_PLACEHOLDER_DOC, encoding="utf-8")
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s8", "status": "done", "description": "合成论文",
         "output_files": ["paper.md"], "expected_output": "paper.md"},
    ]})
    mgr = DeathmatchManager(conv)
    ev = mgr._build_judge_evidence(str(tmp_path))
    assert len(ev) <= ds._JUDGE_EVIDENCE_CHARS
    assert "受限估计：不填报数值" in ev, "大工作区下交付物正文不得被截断挤掉"
    assert "paper.md" in ev


def test_judge_evidence_long_step_ids_keep_excerpt(tmp_path):
    """LLM-authored 长 id/描述不得把交付物摘录挤出证据包。"""
    (tmp_path / "paper.md").write_text(_PLACEHOLDER_DOC, encoding="utf-8")
    steps = [
        {"id": "s" + "x" * 400, "status": "done", "description": "d" * 300,
         "output_files": ["paper.md"], "output_summary": "y" * 500}
        for _ in range(8)
    ]
    steps.append({"id": "s8", "status": "done", "description": "合成",
                  "output_files": ["paper.md"]})
    conv = _conv(deathmatch_plan={"steps": steps})
    mgr = DeathmatchManager(conv)
    ev = mgr._build_judge_evidence(str(tmp_path))
    assert "受限估计：不填报数值" in ev


# ── A3: 提示词规则 ────────────────────────────────────────────────────────

def test_judge_prompt_has_unexecuted_placeholder_rule():
    p = ds.JUDGE_SYSTEM_PROMPT
    assert "未执行占位" in p
    assert "不得判 done" in p or "不得判为 DONE" in p
    assert "continue" in p


def test_verifier_prompt_has_placeholder_check_item():
    p = DeathmatchManager.VERIFIER_SYSTEM_PROMPT
    assert "占位" in p
    assert "待执行" in p or "不填报" in p
    assert "complete" in p


def test_criteria_prompt_requires_executed_results():
    p = DeathmatchManager.CRITERIA_SYSTEM_PROMPT
    assert "实际执行" in p
    assert "待执行" in p or "占位" in p
    # 禁止只覆盖存在性/篇幅
    assert "禁止只覆盖" in p or "不得只覆盖" in p


# ── A4: verifier 证据注入 ────────────────────────────────────────────────

def test_verifier_prompt_injects_deliverable_excerpt(tmp_path):
    ws = str(tmp_path)
    (tmp_path / "empirical.md").write_text(_PLACEHOLDER_DOC, encoding="utf-8")
    conv = _conv(
        deathmatch_plan={"steps": [
            {"id": "s5", "status": "in_progress", "description": "实证分析",
             "expected_output": "empirical.md", "output_files": ["empirical.md"],
             "boundary": ""},
        ]},
        deathmatch_last_verification_result={
            "status": "partial", "workspace_files": [], "completed_steps": [],
        },
    )
    mgr = DeathmatchManager(conv)
    captured = {}

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        captured["prompt"] = user_prompt
        return json.dumps({
            "status": "partial", "issues": ["占位未执行"], "retry_instruction": "",
            "confidence": 0.5, "diagnosis": "结果表未执行", "requires_file": True,
            "continuity_brief": "",
        }, ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    _run(mgr.verify_step_outputs("已产出 empirical.md", ws))
    prompt = captured.get("prompt") or ""
    assert "交付物内容摘录" in prompt
    assert "受限估计：不填报数值" in prompt, "交付物正文必须进入 verifier 证据"
    assert "empirical.md" in prompt


def test_verifier_prompt_excerpt_works_without_declared_output_files(tmp_path):
    """首验（output_files 尚未回填）必须靠本轮 new_files 捕获交付物全文。"""
    ws = str(tmp_path)
    (tmp_path / "empirical.md").write_text(_PLACEHOLDER_DOC, encoding="utf-8")
    conv = _conv(
        deathmatch_plan={"steps": [
            {"id": "s5", "status": "in_progress", "description": "实证分析",
             "expected_output": "empirical.md", "boundary": ""},
        ]},
        deathmatch_last_verification_result={
            "status": "partial", "workspace_files": [], "completed_steps": [],
        },
    )
    mgr = DeathmatchManager(conv)
    captured = {}

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        captured["prompt"] = user_prompt
        return json.dumps({
            "status": "partial", "issues": ["占位未执行"], "retry_instruction": "",
            "confidence": 0.5, "diagnosis": "结果表未执行", "requires_file": True,
            "continuity_brief": "",
        }, ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    _run(mgr.verify_step_outputs("已产出 empirical.md", ws))
    prompt = captured.get("prompt") or ""
    assert "交付物内容摘录" in prompt, "首验（未声明 output_files）必须靠 new_files 命中"
    assert "受限估计：不填报数值" in prompt


# ── B: 守卫指令锚定用户消息 ───────────────────────────────────────────────

def test_search_demand_directive_anchors_user_message():
    from app.services.agent_loop import _search_demand_directive
    q = "你自己评估一下你写的吧，我觉得写的很糟糕，你需要重写实证章节"
    d = _search_demand_directive(q)
    assert "你自己评估一下你写的吧" in d
    assert "不得替代对用户消息本身的处理" in d
    assert "数据无误" in d or "无需修改" in d
    assert "web_search" in d


def test_tool_demand_directive_anchors_user_message():
    from app.services.agent_loop import _tool_demand_directive
    q = "这篇写的很糟糕，请实际修改"
    d = _tool_demand_directive(q)
    assert "这篇写的很糟糕" in d
    assert "不得替代对用户消息本身的处理" in d


def test_guardrail_anchor_empty_query_yields_no_anchor():
    from app.services.agent_loop import _guardrail_user_anchor
    assert _guardrail_user_anchor("") == ""
    assert _guardrail_user_anchor("   ") == ""


# ── C1: classify_intent —— 全 agentic（用户红线，2026-09-20）──────────────

def test_classify_intent_has_no_keyword_enumeration():
    """User directive: intent judgment must be AGENTIC — no hardcoded
    keyword/hint pre-classifier (feedback 2026-07-20). Source guard against
    re-introducing one."""
    import inspect
    src = inspect.getsource(DeathmatchManager.classify_intent)
    for banned in (
        "revision_hints", "discussion_hints", "new_round_markers",
        "hint in q", "hint in query", "hints = [", "in q for",
    ):
        assert banned not in src, f"classify_intent 不得含关键词枚举: {banned}"
    assert "self._make_llm()" in src, "必须始终交给 LLM 判断"
    # LLM prompt must define all four intents.
    for token in ("NEW_ROUND", "REVISE", "DISCUSS", "CLARIFY"):
        assert token in src


def test_classify_intent_accepts_all_four_verdicts():
    """Behavioral pin on the accepted-verdict set (CLARIFY included)."""
    for intent in ("NEW_ROUND", "REVISE", "DISCUSS", "CLARIFY"):
        mgr = DeathmatchManager(_conv())
        mgr._make_llm = lambda **kw: _FakeStreamLLM(f'{{"intent": "{intent}"}}')
        assert _run(mgr.classify_intent("随便一句话", None)) == intent


def test_classify_intent_critique_reaches_llm_prompt():
    """The incident critique must be judged by the LLM with the REVISE
    definition in front of it — and the verdict honored."""
    mgr = DeathmatchManager(_conv())
    q = ("你自己评估一下你写的吧，我觉得写的很糟糕，你仅仅是在疯狂换口径重复我的论点罢了。"
         "真正需要数据的地方全是受限估计：待执行，完全不是一个真正有意义的文章。")
    fake = _FakeStreamLLM('{"intent": "REVISE"}')
    mgr._make_llm = lambda **kw: fake
    assert _run(mgr.classify_intent(q, None)) == "REVISE"
    prompt = fake.last_prompt or ""
    assert "你自己评估一下你写的吧" in prompt
    assert "REVISE" in prompt and "负面评价" in prompt


def test_classify_intent_honors_llm_verdicts():
    q = "通篇没有真实数据支撑，整体返工吧"
    # REVISE
    mgr = DeathmatchManager(_conv())
    mgr._make_llm = lambda **kw: _FakeStreamLLM('{"intent": "REVISE"}')
    assert _run(mgr.classify_intent(q, None)) == "REVISE"
    # NEW_ROUND（即使消息里带返工动词，由 LLM 依目标判断）
    mgr = DeathmatchManager(_conv())
    mgr._make_llm = lambda **kw: _FakeStreamLLM('{"intent": "NEW_ROUND"}')
    assert _run(
        mgr.classify_intent("新任务：重写一份关于新能源产业的报告", None)
    ) == "NEW_ROUND"
    # DISCUSS
    mgr = DeathmatchManager(_conv())
    mgr._make_llm = lambda **kw: _FakeStreamLLM('{"intent": "DISCUSS"}')
    assert _run(mgr.classify_intent("你觉得写得怎么样？", None)) == "DISCUSS"


def test_classify_intent_fail_open_discuss():
    # 空消息：直接 DISCUSS，不调 LLM
    mgr = DeathmatchManager(_conv())
    assert _run(mgr.classify_intent("", None)) == "DISCUSS"
    # LLM 抛错 → DISCUSS
    mgr = DeathmatchManager(_conv())

    class _Boom:
        def stream_chat_structured(self, messages, **kwargs):
            def _gen():
                raise RuntimeError("llm down")
            return _gen()

    mgr._make_llm = lambda **kw: _Boom()
    assert _run(mgr.classify_intent("重写第三章", None)) == "DISCUSS"
    # 非法 intent → DISCUSS
    mgr = DeathmatchManager(_conv())
    mgr._make_llm = lambda **kw: _FakeStreamLLM('{"intent": "EXPLODE"}')
    assert _run(mgr.classify_intent("重写第三章", None)) == "DISCUSS"


class _FakeStreamLLM:
    def __init__(self, text):
        self.text = text
        self.last_prompt = None

    def stream_chat_structured(self, messages, **kwargs):
        self.last_prompt = messages[-1].get("content") if messages else None

        async def _gen():
            yield {"type": "content", "data": self.text}
        return _gen()


# ── C2: begin_revision ───────────────────────────────────────────────────

def test_begin_revision_reactivates_done_round():
    conv = _conv(deathmatch_status="done", deathmatch_context_summary="此前交付摘要")
    mgr = DeathmatchManager(conv)
    assert mgr.begin_revision("写的很糟糕，实证章节全是占位，重写") is True
    assert conv.deathmatch_mode is True
    assert conv.deathmatch_status == "active"
    assert "写的很糟糕" in (conv.deathmatch_context_summary or "")
    assert any(
        e.get("type") == "revision_requested" for e in (conv.deathmatch_events or [])
    ), "必须记录 revision_requested 事件"


def test_begin_revision_noop_when_already_active():
    conv = _conv(deathmatch_status="active")
    mgr = DeathmatchManager(conv)
    assert mgr.begin_revision("继续改") is False


def test_begin_revision_requires_goal():
    conv = _conv(deathmatch_status="done", deathmatch_goal="")
    mgr = DeathmatchManager(conv)
    assert mgr.begin_revision("重写") is False


def test_begin_revision_done_gets_fresh_budget_window():
    """A4.9 Important-2: a done round's clock was not frozen at finalize —
    reopening must not charge the idle period nor gate on exhausted
    turns/wall-time before the revision's first turn."""
    import datetime as _dt
    stale_start = _dt.datetime.utcnow() - _dt.timedelta(days=3)
    conv = _conv(
        deathmatch_status="done",
        deathmatch_wall_time_started_at=stale_start,
        deathmatch_wall_time_used_seconds=1234,
        deathmatch_turns=105,
        deathmatch_max_wall_time_seconds=3600,
        deathmatch_pause_state={"gate": "stale"},
    )
    mgr = DeathmatchManager(conv)
    assert mgr.begin_revision("重写") is True
    assert conv.deathmatch_wall_time_used_seconds == 0
    assert conv.deathmatch_turns == 0
    assert conv.deathmatch_pause_state is None
    started = conv.deathmatch_wall_time_started_at
    assert started is not None
    assert abs((_dt.datetime.utcnow() - started).total_seconds()) < 10


# ── D: chat.py 接线（静态防回退） ─────────────────────────────────────────

def test_chat_routes_wire_revise_to_begin_revision():
    src_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "api", "chat.py"
    )
    with open(src_path, encoding="utf-8") as fh:
        src = fh.read()
    assert src.count('intent == "REVISE"') >= 2
    assert "begin_revision(latest_user_query)" in src
