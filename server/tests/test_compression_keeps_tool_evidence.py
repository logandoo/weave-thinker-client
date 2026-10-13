"""RED tests (A4.8) — 压缩丢弃草稿但保留工具证据 + 客户端清创（2026-09-22，conv 038e837a）。

生产事故（60 箱 conv 038e837a 末轮）：
  ① 常规上下文压缩（183983->114333 tokens）触发 chat.py 压缩处理器按 M3 清空
     `tool_results_accumulated`/`tool_call_events_accumulated`/`search_queries_*`，
     而 agent_loop 的 `state.citation_ledger`（30 条）不随压缩重置 → 落库
     `results[]` 仅剩压缩后的 1 条 web_search（10 条）→ 草稿合法引用
     `[11][17][22]` 在重建账本越界 → sanitize_texts 判「真引用」→ 6 个 [N]
     全剥（3313->3287）→ 前端零角标/卡片，残留 "Management Science2026年"。
  ② 压缩的「丢弃旧稿+重答」只清服务端累加器，不下发 audit_reset —— 客户端按
     M2「SSE 层不可逆」继续显示被丢弃的旧稿，与重答同屏 = 重复生成
     （conv 827a6f78 同族：后端打回已流式草稿必须同步客户端时间轴清创）。

契约：
  A 压缩只丢弃**草稿文本**（assistant_content / content_segments / display_sequence
    的 text 项）；工具证据三件套（tool_results_accumulated /
    tool_call_events_accumulated / search_queries_used / search_queries_by_call）
    必须跨压缩存活——与 audit_reset（conv efaf8f9c）既定政策对齐：工具工作是
    本轮证据，落库 results[]/agent_steps[]/引用映射都依赖它。
  B display_sequence 的 reasoning_step / tool_placeholder 保留（audit_reset A4
    parity：思考与工具不随草稿丢弃）；trailing 思考段落盘为 reasoning_step。
  C 压缩事件下发 `{"audit_reset": True}`——客户端 dropDraftTextAfterLastTool
    清创被丢弃旧稿，用户可见文本与落库一致。

Run: cd backend && python -m pytest tests/test_compression_keeps_tool_evidence.py -v
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_CHAT_SRC = open(
    os.path.join(os.path.dirname(__file__), "..", "app", "api", "chat.py"),
    encoding="utf-8",
).read()

# 压缩事件处理块（到下一个同级 elif 之前）
_COMP_BLOCK_RE = re.compile(
    r'elif "compression" in event:(.*?)(?=\n                            elif ")',
    re.DOTALL,
)


def _compression_block() -> str:
    m = _COMP_BLOCK_RE.search(_CHAT_SRC)
    assert m, "compression 事件处理块不存在"
    return m.group(1)


def test_keeps_tool_evidence_accumulators():
    """A：压缩块不得清空工具证据累加器。"""
    blk = _compression_block()
    for var in (
        "tool_results_accumulated = []",
        "tool_call_events_accumulated = []",
        "search_queries_used = []",
        "search_queries_by_call = {}",
    ):
        assert var not in blk, f"压缩块清空了工具证据累加器：{var}"


def test_drops_draft_text_accumulators():
    """A：草稿文本仍被丢弃（重答以压缩后上下文为准）——cap_released 预工具
    散文除外（A4.9 R2 N1：只保留 _keep_prose 快照）。"""
    blk = _compression_block()
    assert "assistant_content = _keep_prose" in blk
    assert "content_segments = []" in blk


def test_display_sequence_keeps_reasoning_and_tools():
    """B：display_sequence 保留 reasoning/tool，仅去 text。"""
    from app.api.chat import _display_sequence_keep_evidence
    seq = [
        {"type": "reasoning_step", "content": "思考"},
        {"type": "tool_placeholder"},
        {"type": "text", "content": "被丢弃的草稿"},
        {"type": "reasoning_step", "content": "思考2"},
    ]
    kept = _display_sequence_keep_evidence(seq)
    assert [it.get("type") for it in kept] == [
        "reasoning_step", "tool_placeholder", "reasoning_step",
    ]


def test_display_sequence_empty_and_none_safe():
    from app.api.chat import _display_sequence_keep_evidence
    assert _display_sequence_keep_evidence(None) == []
    assert _display_sequence_keep_evidence([]) == []
    assert _display_sequence_keep_evidence([{"type": "text"}]) == []


def test_emits_audit_reset_for_client_timeline_surgery():
    """C：压缩必须下发 audit_reset（客户端清创被丢弃旧稿）。"""
    blk = _compression_block()
    assert '{"audit_reset": True}' in blk


def test_flushes_trailing_reasoning_not_discarded():
    """B：trailing 思考段不静默丢弃（audit_reset parity）。"""
    blk = _compression_block()
    assert "reasoning_step" in blk
    assert "current_reasoning_segment" in blk


def test_display_sequence_keeps_pre_tool_text_before_last_tool():
    """A4.9 R1 Important-3：与客户端 dropDraftTextAfterLastTool 镜像——
    最后一张工具卡之前的 text（cap_released 预工具散文）保留，其后的草稿丢弃。"""
    from app.api.chat import _display_sequence_keep_evidence
    seq = [
        {"type": "text", "content": "已释放的预工具散文"},
        {"type": "tool_placeholder"},
        {"type": "text", "content": "工具后的草稿"},
        {"type": "reasoning_step", "content": "思考"},
    ]
    kept = _display_sequence_keep_evidence(seq)
    assert [it.get("type") for it in kept] == ["text", "tool_placeholder", "reasoning_step"]
    assert kept[0]["content"] == "已释放的预工具散文"


def test_display_sequence_no_tool_drops_all_text():
    """无工具卡时全部 text 均为草稿（客户端同语义）。"""
    from app.api.chat import _display_sequence_keep_evidence
    kept = _display_sequence_keep_evidence([
        {"type": "text", "content": "草稿"},
        {"type": "reasoning_step", "content": "思考"},
    ])
    assert [it.get("type") for it in kept] == ["reasoning_step"]


def test_incident_invariant_results_size_gates_sanitize():
    """事故不变量（行为测试）：落库 results[] 条数决定合法 [N] 是否被剥。
    live ledger 30 条 + results[] 30 条 → [11][17][22] 存活；results[] 10 条
    → 越界被 sanitize_texts 判真引用剥掉（conv 038e837a 事故形态）。"""
    import asyncio
    from app.services.citation_ledger import CitationLedger, build_ledger_from_tool_results

    hits = [
        {"url": f"https://example.com/p{i}", "title": f"来源{i}", "snippet": f"摘要{i}"}
        for i in range(1, 31)
    ]
    live = CitationLedger()
    live.register_hits(hits)
    assert live.size == 30
    draft = "实验见 [11]，期刊见 [17]，细节见 [22]。"
    assert live.verify(draft).cited == {11, 17, 22}

    def results_payload(n):
        return [
            {"id": i, "url": f"https://example.com/p{i}", "title": f"来源{i}", "snippet": ""}
            for i in range(1, n + 1)
        ]

    # 全量 results[]：重建账本 30 条 → 合法引用不动
    full = build_ledger_from_tool_results([{
        "name": "web_search",
        "result": __import__("json").dumps({"results": hits[:30]}),
    }])
    assert full.size == 30
    assert full.verify(draft).unknown == set()

    # 压缩清空后只剩 10 条：[11][17][22] 越界 → sanitize 判真引用剥掉
    shrunk = build_ledger_from_tool_results([{
        "name": "web_search",
        "result": __import__("json").dumps({"results": hits[:10]}),
    }])
    assert shrunk.size == 10
    assert shrunk.verify(draft).unknown == {11, 17, 22}
    out = asyncio.run(shrunk.sanitize_texts([draft]))[0]
    assert "[11]" not in out and "[17]" not in out and "[22]" not in out


def test_released_pre_tool_prose_tracked():
    """A4.9 R2 N1：cap_released 预工具散文需有快照位（压缩只保留它，
    其后草稿仍丢弃——否则「散文+旧稿+重答」三段拼接落库）。"""
    blk = _compression_block()
    assert "_released_prose" in blk
    assert "_keep_prose" in blk


def test_audit_reset_suppressed_in_deathmatch_multiturn():
    """A4.9 R2 N3：死磕多轮流不下发 audit_reset（清创非 turn 作用域，
    会连带删掉上一轮已落库答案文本）——已知边界。"""
    blk = _compression_block()
    assert "_deathmatch_mgr is None" in blk
