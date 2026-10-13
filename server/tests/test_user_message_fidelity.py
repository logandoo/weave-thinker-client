"""RED→GREEN (A4.8): 用户消息保真 — 最新一条逐字全文，历史整条管理（SOTA 对齐）。

conv a104fbc5（2026-09-21）生产事故：用户消息 = 长粘贴（6182 字符表格）
+ 尾部诉求（「…而且那这些受限估计是为什么？」）。协调器（content[:800]）与
审计员（last_user_msg[:800]）只看头部 → 协调器 focus 误判为「请审阅/补全
表格」（chatllm.log 09:32:58）→ 审计员以「答非所问」hard reject 正确答案
（09:35:39 / 09:38:17 两次）→ 出货「未通过全部自动质量检查」警示语。

r1 曾用 head+tail（保头尾切中间）修复 → 用户否决「一定会继续出现类似问题」，
要求对齐 SOTA。核实（codex/opencode 源码）：
  - codex 正常发送路径对 Message 零截断（仅工具输出 middle-out，
    `context_manager/history.rs:466-513`）；用户消息在压缩中按整条
    newest-first 保留 ≤20k tokens（`compact.rs:639-717`）。
  - opencode 用户文本原样进 prompt、无长度上限（`session/message-v2.ts:204-210`）；
    压力由整条消息边界选择与受保护区承担（`session/compaction.ts`）。
契约：**最新用户消息逐字全文，永不切开**；历史消息按整条 token 预算管理，
超预算从最旧整条丢弃并显式披露。

Run: cd backend && ../.venv/bin/python -m pytest tests/test_user_message_fidelity.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.agent_loop import (  # noqa: E402
    _audit_user_facts_text,
    _coordinator_recent_messages,
    _guardrail_user_anchor,
)
from app.services.context_compressor import estimate_text_tokens_rough  # noqa: E402

_AGENT_LOOP_SRC = open(
    os.path.join(os.path.dirname(__file__), "..", "app", "services", "agent_loop.py"),
    encoding="utf-8",
).read()

# a104 形态：长表格粘贴，中段有唯一标记，尾部是真实诉求
_HEAD = "表6-2 基准回归设定与执行状态（样本区间：2018—2025年，重点2022—2025年）\n"
_MID = "MIDDLE_UNIQUE_MARKER_区间说明\n"
_BODY = "命题 被解释变量 核心解释变量 估计方法 标准误与观测数结构 数据来源 识别假设 执行状态\n" * 60
_TAIL = (
    "我觉得你写的很糟糕，仅仅是在重复我的论点，扩写我的论点，没有任何真实的可信的学术文章思维。"
    "而且那这些受限估计是为什么？"
)
_LONG_MSG = _HEAD + _MID + _BODY + _TAIL


class TestLatestUserMessageVerbatim:
    def test_coordinator_latest_message_is_full_fidelity(self):
        recent = _coordinator_recent_messages([{"role": "user", "content": _LONG_MSG}])
        assert recent[-1]["content"] == _LONG_MSG, "最新用户消息被截断 —— 必须逐字全文"
        assert "受限估计是为什么" in recent[-1]["content"]
        assert "MIDDLE_UNIQUE_MARKER" in recent[-1]["content"], "中段也被删除"

    def test_anchor_carries_full_message(self):
        text = "这是一条足够长的用户消息，" + "中间内容" * 40 + "，最后我的诉求是：为什么受限估计？"
        assert _guardrail_user_anchor(text) == f"用户最新消息：{text}\n"

    def test_anchor_preserves_whitespace_verbatim(self):
        # A4.9 F3：空白折叠（" ".join(split())）也是字符删除——换行/制表符必须保真
        text = "第一行：粘贴的表格\n\t型号\t数量\n第二行：\n\n最后诉求：为什么受限估计？"
        out = _guardrail_user_anchor(text)
        assert out == f"用户最新消息：{text}\n"
        assert "\n\t" in out and "\n\n" in out

    def test_facts_exclude_latest_and_join_verbatim(self):
        older = "我之前的背景：工具有限，预算 5 万元。"
        out = _audit_user_facts_text([
            {"role": "user", "content": older},
            {"role": "user", "content": _LONG_MSG},
        ])
        assert out == older, "背景事实必须逐字（且不含最新一条的重复）"
        assert _LONG_MSG not in out, "最新一条已 headline 展示，不应重复注入"


class TestHistoryWholeItemBudget:
    def test_over_budget_drops_oldest_whole_message_with_disclosure(self):
        old = "OLDEST_UNIQUE_" + "甲" * 400
        mid = "MIDMESSAGE_UNIQUE_" + "乙" * 400
        latest = "LATEST_UNIQUE_问题"
        recent = _coordinator_recent_messages(
            [{"role": "user", "content": old},
             {"role": "assistant", "content": mid},
             {"role": "user", "content": latest}],
            token_budget=50,
        )
        # A4.9 F1：披露必须是独立前导 system 消息，绝不改写任何用户消息
        assert recent[0]["role"] == "system"
        assert "未纳入本路由上下文" in recent[0]["content"]
        assert recent[-1]["role"] == "user"
        assert recent[-1]["content"] == latest, "最新一条必须逐字全文（零改写）"
        joined = "\n".join(m["content"] for m in recent[1:])
        assert "OLDEST_UNIQUE_" not in joined, "旧消息整条丢弃（不留残片）"
        assert "MIDMESSAGE_UNIQUE_" not in joined

    def test_latest_survives_even_when_alone_over_budget(self):
        latest = "LATEST_OVER_BUDGET_" + "丙" * 800
        recent = _coordinator_recent_messages(
            [{"role": "user", "content": latest}], token_budget=1)
        assert len(recent) == 1 and recent[-1]["content"] == latest

    def test_latest_user_outside_message_window_is_recovered(self):
        # A4.9 F2：`messages[-N:]` 窗口不得吞掉本轮最新用户消息
        latest = "UNIQUE_LATEST_QUESTION_为什么"
        msgs = [{"role": "user", "content": latest}]
        msgs += [{"role": "assistant", "content": f"a{i}"} for i in range(8)]
        recent = _coordinator_recent_messages(msgs, max_messages=6)
        assert any(m["content"] == latest for m in recent), "最新用户消息在窗口外被丢弃"

    def test_all_assistant_window_does_not_crash(self):
        # A4.9 F2：全是 assistant 的窗口（无 user）不得 IndexError；预算极小时
        # 允许只返回披露消息（无消息可纳入），但不得崩溃
        msgs = [{"role": "assistant", "content": f"a{i}" * 400} for i in range(8)]
        recent = _coordinator_recent_messages(msgs, max_messages=3, token_budget=10)
        assert isinstance(recent, list)
        assert {m["role"] for m in recent} <= {"assistant", "system"}
        assert not any(m["role"] == "user" for m in recent)

    def test_skip_is_contiguous_from_oldest(self):
        # A4.9 r2 N2：必须有「超预算项之后仍可容纳的更旧小消息」才能鉴别
        # break（连续截断）vs skip-one（跳过超预算项继续扫更旧）
        tiny_oldest = "TINY_OLDEST_应被连续截断"
        huge = "HUGE_OVER_BUDGET_" + "壬" * 400
        newer = "NEWER_FITS_" + "癸" * 100
        latest = "LATEST_Q"
        recent = _coordinator_recent_messages(
            [{"role": "user", "content": tiny_oldest},
             {"role": "assistant", "content": huge},
             {"role": "user", "content": newer},
             {"role": "user", "content": latest}],
            token_budget=260,
        )
        contents = [m["content"] for m in recent]
        assert latest in contents
        assert newer in contents, "较新消息在预算内应保留"
        assert all(tiny_oldest not in c for c in contents), (
            "skip-one 实现会保留超预算项之后的更旧小消息 —— 违反连续截断契约")
        assert all(huge not in c for c in contents), "超预算消息整条丢弃（不留残片）"

    def test_latest_user_survives_when_newer_candidate_exhausts_budget(self):
        # A4.9 r2 N1：窗口补回的最新 user 位于更旧位置 + 更新的超大候选耗尽
        # 预算 → break 也必须显式保回最新 user（「永不丢」不变量优先）
        latest = "RECOVERED_LATEST_Q"
        huge = "HUGE_NEWER_" + "子" * 20000
        recent = _coordinator_recent_messages(
            [{"role": "user", "content": latest},
             {"role": "assistant", "content": huge}],
            max_messages=1, token_budget=100,
        )
        assert any(m["content"] == latest for m in recent), "补回的最新 user 被预算 break 丢弃"

    def test_in_budget_history_is_verbatim(self):
        old = "OLD_完整保留"
        latest = "NEW_问题"
        recent = _coordinator_recent_messages(
            [{"role": "user", "content": old},
             {"role": "user", "content": latest}],
            token_budget=10000,
        )
        assert [m["content"] for m in recent] == [old, latest]

    def test_tool_and_empty_messages_filtered(self):
        recent = _coordinator_recent_messages([
            {"role": "tool", "content": "x" * 5000},
            {"role": "user", "content": "   "},
            {"role": "user", "content": "问题"},
        ])
        assert [m["content"] for m in recent] == ["问题"]


class TestFactsWholeItemBudget:
    def test_over_budget_fact_skipped_whole_with_disclosure(self):
        oldest = "FACT_OLDEST_UNIQUE_" + "丁" * 800
        newer = "FACT_NEWER_背景：预算 5 万"
        out = _audit_user_facts_text(
            [{"role": "user", "content": oldest},
             {"role": "user", "content": newer},
             {"role": "user", "content": "LATEST_问题"}],
            token_budget=100,
        )
        assert newer in out
        assert "FACT_OLDEST_UNIQUE_" not in out, "超预算事实必须整条跳过（不得切半）"
        assert "未纳入：上下文预算" in out

    def test_all_skipped_still_discloses(self):
        # A4.9 F4：全部未纳入也不得静默——必须返回披露文本（调用方据此注入）
        big = "FACT_ALL_SKIPPED_" + "辛" * 4000
        out = _audit_user_facts_text(
            [{"role": "user", "content": big},
             {"role": "user", "content": "LATEST_问题"}],
            token_budget=10,
        )
        assert out, "背景事实全部未纳入时静默返回空 = 违规"
        assert "未纳入：上下文预算" in out
        assert "FACT_ALL_SKIPPED_" not in out

    def test_cap_limited_disclosure_names_the_reason(self):
        # A4.9 F5：条数上限导致的跳过不得谎称「上下文预算」
        msgs = [{"role": "user", "content": f"FACT_{i}"} for i in range(5)]
        out = _audit_user_facts_text(msgs, max_items=2, token_budget=10000)
        assert "FACT_3" in out and "FACT_2" in out
        assert "FACT_4" not in out, "最新一条不注入背景事实"
        assert "未纳入：条数上限" in out

    def test_only_latest_yields_empty(self):
        assert _audit_user_facts_text([{"role": "user", "content": _LONG_MSG}]) == ""


class TestBudgetUnauditedDisclosure:
    def test_consume_returns_budget_note_once(self):
        # A4.9 F7：审计因上下文预算未执行 → 出货说明（且一次性消费）
        from app.services.agent_loop import (
            _AUDIT_BUDGET_UNAUDITED_NOTE,
            _consume_low_confidence_note,
        )

        class _S:
            audit_low_confidence = False
            audit_chunk_coverage_incomplete = False
            audit_budget_unaudited = True

        s = _S()
        note = _consume_low_confidence_note(s)
        assert note == _AUDIT_BUDGET_UNAUDITED_NOTE
        assert s.audit_budget_unaudited is False
        assert _consume_low_confidence_note(s) is None


class TestWiring:
    def test_no_middle_cut_helpers_or_head_only_slices(self):
        # 用户消息路径不得再有中段切除/head-only 接线。草稿审计窗（_draft_audit_view）
        # 的「头+尾」是既有独立设计（分块声称清单覆盖中段），不在本契约范围。
        for banned in ("_head_tail_window", "_user_message_window",
                       "last_user_msg[:800]", "content[:800]", "_uc[:300]"):
            assert banned not in _AGENT_LOOP_SRC, f"中段切除/head-only 残留：{banned}"

    def test_auditor_uses_verbatim_latest_message(self):
        assert "用户最新消息：{last_user_msg}" in _AGENT_LOOP_SRC

    def test_budget_uses_cjk_aware_estimator(self):
        # 预算管理必须用 CJK 感知估算（禁止字符数拍脑袋阈值）
        assert "estimate_text_tokens_rough" in _AGENT_LOOP_SRC
        assert estimate_text_tokens_rough("中文" * 10) == 20

    def test_fail_open_paths_set_budget_unaudited_flag(self):
        # A4.9 F7 设置侧接线：超预算时 fail-open 路径（exception/bad_json/
        # non_dict + 2026-10-06 分块覆盖未达成）都必须置位未审计标记
        # （消费侧见 TestBudgetUnauditedDisclosure）
        assert _AGENT_LOOP_SRC.count("state.audit_budget_unaudited = True") == 4
        assert _AGENT_LOOP_SRC.count("_audit_over_budget") >= 4
