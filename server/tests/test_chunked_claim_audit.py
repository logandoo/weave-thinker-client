"""TDD tests: 审计窗口 SOTA 重构 v1 — 分块声称清单（全稿覆盖）替代窗口加宽。

设计依据（2026-09-20 用户质询）：
- opencode/codex：有界窗口 + 检查点 + 独立核查/子代理，绝不把全量塞进单次调用；
- LongJudgeBench：长稿评审随长度退化，document-level 判定需独立全局视角；
- LLM×MapReduce：分块结构化输出 + 合并（防 chunk 间依赖/冲突）；
- Gavel：逐块处理 recall 高但错误累积 → 证据可追溯 + 确定性合并。

v1：长稿（超窗口）走 map（逐块原子声称+局部风险）→ 有界清单+覆盖率 →
审计员按清单核验中段；未覆盖段 → 低置信出货底线（绝不静默合格）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_chunked_claim_audit.py -v
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.agent_loop import (  # noqa: E402
    AgentLoop,
    _format_chunk_inventory,
    _parse_chunk_claim_json,
    _split_audit_chunks,
)


def _run(coro):
    return asyncio.run(coro)


def test_split_chunks_paragraph_target_and_full_coverage():
    draft = "\n\n".join([f"段落{i}：" + "内容" * 300 for i in range(12)])
    chunks, omitted = _split_audit_chunks(
        draft, target_tokens=800, overlap_chars=0, max_chunks=50)
    assert omitted == 0
    assert len(chunks) > 1
    for i in range(12):
        assert any(f"段落{i}：" in c for c in chunks), f"段落{i} 丢失"
    from app.services.context_compressor import estimate_text_tokens_rough as est
    assert all(est(c) <= 800 * 1.6 for c in chunks), "分块必须有界"


def test_split_chunks_long_paragraph_and_cap():
    draft = "甲" * 20000
    chunks, omitted = _split_audit_chunks(
        draft, target_tokens=1000, overlap_chars=0, max_chunks=5)
    assert len(chunks) == 5
    assert omitted > 0, "超过块数上限必须显式计数（未覆盖 ≠ 无问题）"
    assert chunks[0] and chunks[0] in draft


def test_split_chunks_overlap_applied():
    draft = "\n\n".join(["甲" * 3000, "乙" * 3000])
    chunks, _ = _split_audit_chunks(
        draft, target_tokens=500, overlap_chars=50, max_chunks=50)
    assert len(chunks) >= 2
    # 第二块以第一块尾部 50 字符重叠开头
    assert chunks[1].startswith(chunks[0][-50:])


def test_parse_chunk_claim_json_tolerant():
    assert _parse_chunk_claim_json('```json\n{"claims": []}\n```')["claims"] == []
    assert _parse_chunk_claim_json(
        '前言 {"claims": [{"claim":"x"}]} 结尾')["claims"][0]["claim"] == "x"
    assert _parse_chunk_claim_json("not json") is None
    assert _parse_chunk_claim_json("") is None


def test_format_inventory_marks_evidence_and_coverage():
    claims = [{"claim": "GDP 增速为 5.0%"}, {"claim": "未知实体 XZ-9 存在"}]
    evidence = "2025 年 GDP 增速 5.0%"
    block = _format_chunk_inventory(
        claims,
        {"chunks_total": 3, "chunks_done": 2, "omitted_chunks": 0,
         "flags": {"off_topic_chunks": 1}},
        evidence,
    )
    assert "覆盖 2/3 段" in block
    assert "未覆盖" in block and "不得据此判合格" in block
    assert "证据在场" in block and "未见对应证据" in block
    assert "与问题无关" in block


def test_format_inventory_cap_disclosure():
    claims = [{"claim": f"声称{i} 数字{i}"} for i in range(100)]
    block = _format_chunk_inventory(
        claims, {"chunks_total": 1, "chunks_done": 1, "omitted_chunks": 0}, "无")
    assert "另有 20 条声称未列出" in block and "未列出 ≠ 无问题" in block


# ── map 阶段：逐块提取（fake LLM）─────────────────────────────────────────

def _bare_loop(fake_complete):
    loop = AgentLoop.__new__(AgentLoop)
    loop.audit_llm = None
    loop.coordinator_llm = None
    loop._complete_json = fake_complete
    return loop


def test_chunked_inventory_merges_and_dedups():
    draft = "\n\n".join(["甲" * 3000, "乙" * 3000])

    async def fake_complete(msgs, temperature=0.0, llm=None):
        body = msgs[-1]["content"]
        # 注意：块间有 overlap，第二块可能同时含上一块尾字（甲）——先判乙
        if "乙" in body:
            return ('```json\n{"claims":[{"claim":"声称A","quote":"qa"},'
                    '{"claim":"声称C","quote":"qc"}],"flags":{}}\n```')
        return ('{"claims":[{"claim":"声称A","quote":"qa"},'
                '{"claim":"声称B","quote":"qb"}],"flags":{"off_topic":true}}')

    loop = _bare_loop(fake_complete)
    claims, cov = _run(loop._chunked_claim_inventory(draft))
    names = [c["claim"] for c in claims]
    assert names.count("声称A") == 1, "跨块重复声称必须去重"
    assert set(names) == {"声称A", "声称B", "声称C"}
    assert cov["chunks_done"] == cov["chunks_total"] >= 2
    assert cov["flags"]["off_topic_chunks"] == 1


def test_chunked_inventory_failed_chunk_is_explicit():
    draft = "\n\n".join(["甲" * 3000, "乙" * 3000])

    async def fake_complete(msgs, temperature=0.0, llm=None):
        if "乙" in msgs[-1]["content"]:
            raise RuntimeError("provider down")
        return '{"claims":[{"claim":"声称A","quote":"qa"}],"flags":{}}'

    loop = _bare_loop(fake_complete)
    claims, cov = _run(loop._chunked_claim_inventory(draft))
    assert cov["chunks_done"] < cov["chunks_total"], "失败块必须显式计入未覆盖"
    assert [c["claim"] for c in claims] == ["声称A"]


def test_audit_wires_chunked_full_audit_and_confidence_floor():
    """2026-10-06 用户红线（conv ae9aa092）：草稿窗口废除后，声称清单不再是
    窗口补偿件（草稿全文/分块全覆盖直接可见）；审计主流程改为接线
    `_chunked_full_audit`（分块全覆盖审计，零省略）。清单函数本体保留可用。"""
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "app", "services", "agent_loop.py"),
        encoding="utf-8",
    ).read()
    assert "_chunked_full_audit(" in src, "审计主流程必须接线分块全覆盖审计"
    assert "audit_chunk_coverage_incomplete" in src, "分块覆盖缺口必须显式置位"
    assert "audit_low_confidence = True" in src


# ── r1 复审收口：超时保留/模式校验/零声称/token-less/说明文案 ────────────

def test_timeout_retains_completed_chunks(monkeypatch):
    """A4.9 r1 Important-2：聚合超时不得丢弃已完成块。"""
    import app.services.agent_loop as al

    draft = "\n\n".join(["甲" * 3000, "乙" * 3000])

    async def fake_complete(msgs, temperature=0.0, llm=None):
        if "甲" in msgs[-1]["content"] and "乙" not in msgs[-1]["content"]:
            await asyncio.sleep(5)
            return '{"claims":[{"claim":"慢块声称","quote":"q"}],"flags":{}}'
        return '{"claims":[{"claim":"快块声称","quote":"q"}],"flags":{}}'

    loop = _bare_loop(fake_complete)
    _orig_wait = asyncio.wait

    async def tiny_wait(tasks, timeout=None):
        return await _orig_wait(tasks, timeout=0.05)

    monkeypatch.setattr(al.asyncio, "wait", tiny_wait)
    claims, cov = _run(loop._chunked_claim_inventory(draft))
    names = [c["claim"] for c in claims]
    assert "快块声称" in names, "超时后已完成块的结果被丢弃"
    assert cov["chunks_done"] >= 1
    assert cov["chunks_done"] < cov["chunks_total"], "超时块必须计入未覆盖"


def test_schema_junk_chunk_not_counted_done():
    """A4.9 r1 Important-3：拒绝/{}/错形状不计 done（零声称块另计）。"""
    draft = "\n\n".join(["甲" * 3000, "乙" * 3000])

    async def fake_complete(msgs, temperature=0.0, llm=None):
        if "乙" in msgs[-1]["content"]:
            return '{"refusal": "cannot"}'
        return '{"claims": [], "flags": {}}'

    loop = _bare_loop(fake_complete)
    claims, cov = _run(loop._chunked_claim_inventory(draft))
    assert cov["chunks_done"] == 1, "错形状块不得计 done"
    assert cov["empty_claim_chunks"] >= 1, "零声称块必须显式计数"
    assert claims == []


def test_inventory_tokenless_claim_not_marked_present():
    """A4.9 r1 Important-4：无核对 token 的声称不得标『证据在场』。"""
    block = _format_chunk_inventory(
        [{"claim": "用户偏好深色主题"}],
        {"chunks_total": 1, "chunks_done": 1, "omitted_chunks": 0}, "任意证据")
    assert "无法核对（无可用核对 token）" in block
    assert "证据在场" not in block


def test_inventory_zero_claim_chunk_note():
    block = _format_chunk_inventory(
        [],
        {"chunks_total": 2, "chunks_done": 2, "omitted_chunks": 0,
         "empty_claim_chunks": 1}, "证据")
    assert "零声称 ≠ 已核验干净" in block


def test_low_confidence_note_helper_semantics(monkeypatch):
    import app.services.agent_loop as al

    class _S:
        audit_low_confidence = False
        audit_chunk_coverage_incomplete = False

    st = _S()
    st.audit_chunk_coverage_incomplete = True
    note = al._consume_low_confidence_note(st)
    assert note and "分块覆盖不完整" in note
    assert "均已确认在来源资料中有出处" not in note, "未覆盖段不得声称已确认"
    assert st.audit_chunk_coverage_incomplete is False
    # 开关关闭 → 静默且清标记
    st2 = _S()
    st2.audit_low_confidence = True
    monkeypatch.setattr(al.config, "_config", {**getattr(al.config, "_config", {})})
    monkeypatch.setattr(
        type(al.config), "agent_audit_low_confidence_note_enabled",
        property(lambda self: False), raising=False)
    assert al._consume_low_confidence_note(st2) is None


def test_ship_sites_no_direct_note_yield():
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "app", "services", "agent_loop.py"),
        encoding="utf-8",
    ).read()
    assert 'yield {"content": _AUDIT_LOW_CONFIDENCE_NOTE}' not in src
    assert 'yield {"content": _AUDIT_CHUNK_COVERAGE_NOTE}' not in src
    assert src.count("_consume_low_confidence_note(state)") >= 4


# ── 残留清扫：缓存 / 引文回验 / advisory 覆盖说明 ────────────────────────

def test_inventory_cache_avoids_repeat_map_calls():
    draft = "\n\n".join(["甲" * 3000, "乙" * 3000])
    calls = {"n": 0}

    async def fake_complete(msgs, temperature=0.0, llm=None):
        calls["n"] += 1
        return '{"claims":[{"claim":"声称X","quote":"甲"}],"flags":{}}'

    loop = _bare_loop(fake_complete)
    claims1, cov1 = _run(loop._chunked_claim_inventory(draft))
    n1 = calls["n"]
    claims2, cov2 = _run(loop._chunked_claim_inventory(draft))
    assert calls["n"] == n1, "完整覆盖结果必须按草稿 hash 缓存（不得重复 map）"
    assert [c["claim"] for c in claims1] == [c["claim"] for c in claims2]
    assert cov1 == cov2


def test_inventory_incomplete_not_cached():
    draft = "\n\n".join(["甲" * 3000, "乙" * 3000])
    calls = {"n": 0}

    async def fake_complete(msgs, temperature=0.0, llm=None):
        calls["n"] += 1
        if "乙" in msgs[-1]["content"]:
            raise RuntimeError("down")
        return '{"claims":[{"claim":"声称X","quote":"甲"}],"flags":{}}'

    loop = _bare_loop(fake_complete)
    _claims, cov1 = _run(loop._chunked_claim_inventory(draft))
    assert cov1["chunks_done"] < cov1["chunks_total"]
    n1 = calls["n"]
    _run(loop._chunked_claim_inventory(draft))
    assert calls["n"] > n1, "不完整结果不得缓存（下一审计点可重试）"


def test_quote_verification_flags_ungrounded_quote():
    draft = "\n\n".join(["甲" * 3000, "乙" * 3000])

    async def fake_complete(msgs, temperature=0.0, llm=None):
        if "乙" in msgs[-1]["content"]:
            # 引文在该块中不存在 → anomaly
            return ('{"claims":[{"claim":"声称B","quote":"这段文字不在片段里出现"}],'
                    '"flags":{}}')
        return ('{"claims":[{"claim":"声称A","quote":"甲甲甲甲甲甲甲甲甲甲"}],'
                '"flags":{}}')

    loop = _bare_loop(fake_complete)
    claims, cov = _run(loop._chunked_claim_inventory(draft))
    assert cov["anomaly_claims"] == 1
    flagged = [c for c in claims if c["quote_verified"] is False]
    assert [c["claim"] for c in flagged] == ["声称B"]
    block = _format_chunk_inventory(claims, cov, "证据")
    assert "无法在来源片段中定位" in block


def test_quote_grounded_helper():
    from app.services.agent_loop import _quote_grounded
    assert _quote_grounded("", "任意") is True
    assert _quote_grounded("短", "短") is True
    assert _quote_grounded("甲 乙 丙 丁 戊 己 庚 辛", "甲 乙 丙 丁 戊 己 庚 辛 壬") is True
    assert _quote_grounded("完全没有出现过的引文内容", "别的文本") is False


def test_advisory_branch_consumes_coverage_only_note():
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "app", "services", "agent_loop.py"),
        encoding="utf-8",
    ).read()
    assert "coverage_only=True" in src, "advisory 分支必须消费覆盖说明"


def test_note_helper_coverage_only_mode(monkeypatch):
    import app.services.agent_loop as al

    class _S:
        audit_low_confidence = True
        audit_chunk_coverage_incomplete = False

    # coverage_only：仅存在性低置信 → 不返回（advisory 软判保持静默）
    st = _S()
    assert al._consume_low_confidence_note(st, coverage_only=True) is None
    assert st.audit_low_confidence is False  # 标记仍被清理
    # coverage_only：覆盖不完整 → 返回覆盖文案
    st2 = _S()
    st2.audit_chunk_coverage_incomplete = True
    note = al._consume_low_confidence_note(st2, coverage_only=True)
    assert note and "分块覆盖不完整" in note


def test_advisory_coverage_note_emitted_in_both_branches():
    """A4.9 r3 Important：draft 与 synthesis 两个 advisory 分支都必须消费覆盖说明。"""
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "app", "services", "agent_loop.py"),
        encoding="utf-8",
    ).read()
    assert src.count("coverage_only=True") >= 2


def test_quote_grounded_typographic_variants():
    from app.services.agent_loop import _quote_grounded
    quote = "“GDP 增速为 5.0%……”"
    chunk = "2025 年 GDP 增速为 5.0%，价格低位。"
    assert _quote_grounded(quote, chunk) is True, "排版变体（引号/省略号）归一化后应可定位"
    assert _quote_grounded("完全不同的引文内容甲乙丙丁", chunk) is False
