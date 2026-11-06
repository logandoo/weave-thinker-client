"""TDD — W1 M2a 缺证据显式声明 marker + M2b recall log miss 遥测
（论文 2609.37559 APM-Bench 采纳件，docs/AUDIT_2026-10-13_deathmatch_memory_papers.md §3.2/§10）。

M2a：显式记忆线索（时间/偏好等）+ 三层 above-floor 零命中 → 注入上下文尾部
追加 1 行确定性声明（「记忆检索状态」元块，不计预算/不计引用）。
A6 勘正：零命中 ≠ 空 ctx（总览基底恒定注入）——条件=线索 ∧ above-floor 命中数=0。
M2b：recall log 增 miss / providers_queried 元数据字段（评测台统计零命中率）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_memory_missing_evidence_notice.py -v
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.memory_retrieval_service as mrs


# ── M2a：线索判定（确定性正则，检索侧提示——非判断闸） ──────────────────

def test_memory_cue_detects_time_and_preference_words():
    for q in ["我上次说的是什么", "你记得我的偏好吗", "之前聊过的方案", "我习惯用 Vim",
              "昨天提到的那本书"]:
        assert mrs._has_memory_cue(q), f"应识别记忆线索: {q}"


def test_memory_cue_rejects_plain_queries():
    for q in ["帮我写一段 Python 排序", "今天天气怎么样", "解释一下快速排序", ""]:
        assert not mrs._has_memory_cue(q), f"不应识别为记忆线索: {q}"


# ── M2a：marker 生成（纯函数缝） ───────────────────────────────────────

def test_marker_emitted_on_cue_and_zero_hits():
    note = mrs._missing_evidence_notice("我上次说的偏好是什么", has_hits=False, enabled=True)
    assert note, "线索 + 零命中必须出声明"
    assert "未找到与该问题直接相关的条目" in note
    assert "记忆检索状态" in note


def test_marker_suppressed_without_cue():
    assert not mrs._missing_evidence_notice("写个排序函数", has_hits=False, enabled=True)


def test_marker_suppressed_with_hits():
    assert not mrs._missing_evidence_notice("我上次说的偏好是什么", has_hits=True, enabled=True)


def test_marker_suppressed_when_disabled():
    assert not mrs._missing_evidence_notice("我上次说的偏好是什么", has_hits=False, enabled=False)


def test_marker_is_single_line():
    note = mrs._missing_evidence_notice("之前提过什么", has_hits=False, enabled=True)
    assert note.count("\n") <= 1, "marker 限 1 行"


# ── M2b：recall log 元数据（miss / providers_queried） ──────────────────

def test_emit_recall_log_records_miss_and_providers(monkeypatch):
    captured = {}

    def fake_record(**kwargs):
        captured.update(kwargs)

    import app.services.memory_recall_log_service as rls
    monkeypatch.setattr(rls, "record_recall_log_bg", fake_record, raising=False)
    # _emit_recall_log 在函数内 from-import —— 打补丁到源模块属性即可生效
    stats = {"tier_scores": {}, "budget_chars": 2000, "truncated": False,
             "cache_hit": False,
             "providers_queried": ["stage0", "stage1_bm25", "stage3_embedding"]}
    mrs._emit_recall_log("u1", "conv1", [{"role": "user", "content": "我上次说的"}],
                         ctx="", ids=[], top=0.0, stats=stats, elapsed_ms=12.0)
    assert captured.get("miss") is True, "零注入候选必须记 miss=True"
    # M1（复审）：providers=实际被查询管线段（零命中行也有意义），非命中层
    assert captured.get("providers_queried") == [
        "stage0", "stage1_bm25", "stage3_embedding"]


def test_emit_recall_log_miss_false_when_injected(monkeypatch):
    captured = {}

    def fake_record(**kwargs):
        captured.update(kwargs)

    import app.services.memory_recall_log_service as rls
    monkeypatch.setattr(rls, "record_recall_log_bg", fake_record, raising=False)
    stats = {"tier_scores": {"episodic": [["e1", 0.6]]},
             "providers_queried": ["stage0", "temporal_list"]}
    mrs._emit_recall_log("u1", None, [{"role": "user", "content": "q"}],
                         ctx="x", ids=["e1"], top=0.6, stats=stats, elapsed_ms=5.0)
    assert captured.get("miss") is False
    assert captured.get("providers_queried") == ["stage0", "temporal_list"]


def test_clip_preserving_notice_keeps_marker_tail():
    note = mrs._missing_evidence_notice("之前提过什么", has_hits=False, enabled=True)
    body = "记忆正文" * 200
    ctx = body + note
    out = mrs.clip_preserving_notice(ctx, 100)
    assert len(out) <= 100
    assert "未找到与该问题直接相关的条目" in out, "尾部缺证据声明必须随钳位保留"
    # 无 marker 时普通从头截断语义不变
    assert mrs.clip_preserving_notice(body, 10) == body[:10]


def test_marker_default_on_when_key_absent():
    """M2（复审）：缺 key 时默认开（D-18）——不许靠显式 set 才绿。"""
    import app.core.config as _cfg
    cfg = _cfg.get_config()
    mem = cfg._config.get("memory") or {}
    assert cfg.memory.get("missing_evidence_notice_enabled", True) is True
    assert "missing_evidence_notice_enabled" not in mem or mem.get("missing_evidence_notice_enabled") in (True, 1)


def test_config_default_wiring_not_tautological(monkeypatch):
    """M2 空真修正（复审 NEW-5）：断言「缺 key 时调用侧默认开」的**行为**，
    不钉 dict.get 默认参（那是同义反复）。"""
    import app.core.config as _cfg
    cfg = _cfg.get_config()
    mem = cfg._config.setdefault("memory", {})
    prev = mem.pop("missing_evidence_notice_enabled", "___absent___")
    try:
        # NEW-7（复审）：直接调生产接线 _finalize_ctx_with_notice——它内部
        # 读 config.memory.get(k, True)（调用侧默认），断言行为而非表达式副本。
        out = mrs._finalize_ctx_with_notice("记忆正文", "之前提过什么", False)
        assert "未找到与该问题直接相关的条目" in out, "缺 key 时默认必须开（D-18，生产接线）"
        mem["missing_evidence_notice_enabled"] = False
        out2 = mrs._finalize_ctx_with_notice("记忆正文", "之前提过什么", False)
        assert "未找到与该问题直接相关的条目" not in out2
    finally:
        if prev == "___absent___":
            mem.pop("missing_evidence_notice_enabled", None)
        else:
            mem["missing_evidence_notice_enabled"] = prev


def test_finalize_ctx_notice_idempotent():
    """NEW-6：出口 wrap 幂等——已有声明块不得二次追加（冷启动双 wrap 面）。"""
    body = "记忆正文"
    once = mrs._finalize_ctx_with_notice(body, "之前提过什么", False)
    twice = mrs._finalize_ctx_with_notice(once, "之前提过什么", False)
    assert twice.count("<记忆检索状态>") == 1
