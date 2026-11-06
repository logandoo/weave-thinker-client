"""TDD tests: 审计链截断完整性修复（2026-09-20 用户审计要求）。

用户原则（memory/feedback_information_integrity_over_tokens.md）：
信息完整性 >> 省 token；截断必须显式标注、可回读、且"看不到"必须走 unverifiable，
不得造成"编造"误杀。

覆盖：
- F1 terminal 输出不再静默字符截断（全量返回，字节硬顶有标注）
- F2 非 grounding 证据片段标注"截断"（触发既有硬性约束）+ 归档全文回读进确定性语料
- F3 死磕 judge：回复片段/证据包 头+尾（尾部证据不丢）
- F4 死磕 verifier：expected_output 显式 + 步骤 JSON/摘要截断标注
- F5 工作区快照 400 上限显式披露总数与"未列出 ≠ 不存在"
- F6 记忆注入逐条截断带省略号标注
- F8 审计历史窗（6 条）截断披露（未展示 ≠ 不存在）

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_integrity_truncation_fixes.py -v
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.agent_loop import (  # noqa: E402
    ToolCallResult,
    _build_audit_evidence,
)


class _State:
    def __init__(self, tool_results, messages=None):
        self.tool_results = tool_results
        self.messages = messages or []


def _tr(i, name, result, error=False):
    return ToolCallResult(call_id=f"c{i}", name=name, arguments={}, result=result, error=error)


def _run(coro):
    return asyncio.run(coro)


# ── F1: terminal 不得静默字符截断 ─────────────────────────────────────────

def _make_ws():
    """在合法 workspace_root 下建临时工作区（terminal 沙箱校验）。"""
    import shutil
    import uuid as _uuid
    from app.core.config import get_config
    ws = Path(get_config().workspace_root) / f"pytest_integrity_{_uuid.uuid4().hex[:8]}"
    ws.mkdir(parents=True, exist_ok=True)
    return ws, shutil.rmtree


def test_terminal_large_output_not_silently_cut():
    ws, cleanup = _make_ws()
    try:
        (ws / "big.txt").write_text("A" * 15000, encoding="utf-8")
        import app.tools.terminal as term
        out = _run(term.terminal(
            {"command": "cat big.txt", "timeout": 30},
            workspace_path=str(ws),
        ))
        data = json.loads(out)
        stdout = data.get("stdout", "")
        assert "A" * 15000 in stdout, (
            f"terminal 静默截断输出（{len(stdout)} 字符）——违反信息完整性原则"
        )
    finally:
        cleanup(ws, ignore_errors=True)


def test_terminal_large_stream_persisted_with_pointer():
    """30k–100k 区间：不得原样灌入上下文，须全文存档 + 指针（无损可回读）。"""
    ws, cleanup = _make_ws()
    try:
        (ws / "huge.txt").write_text("B" * 40000, encoding="utf-8")
        import app.tools.terminal as term
        out = _run(term.terminal(
            {"command": "cat huge.txt", "timeout": 30},
            workspace_path=str(ws),
        ))
        data = json.loads(out)
        stdout = data.get("stdout", "")
        assert "Full output saved to:" in stdout, "超阈值输出必须存档并给指针"
        assert len(stdout) < 40000, "存档后模型侧只应看到预览信封"
        archive = stdout.split("Full output saved to:")[1].splitlines()[0].strip()
        assert Path(archive).is_file()
        assert Path(archive).read_text(encoding="utf-8") == "B" * 40000
    finally:
        cleanup(ws, ignore_errors=True)


def test_terminal_timeout_output_not_silently_cut():
    ws, cleanup = _make_ws()
    try:
        (ws / "big.txt").write_text("C" * 15000, encoding="utf-8")
        import app.tools.terminal as term
        out = _run(term.terminal(
            {"command": "cat big.txt && sleep 30", "timeout": 5},
            workspace_path=str(ws),
        ))
        data = json.loads(out)
        assert data.get("timed_out") is True
        stdout = data.get("stdout", "")
        assert "C" * 15000 in stdout or "截断" in stdout, (
            "超时路径同样不得静默截断（要么全量，要么显式标注）"
        )
    finally:
        cleanup(ws, ignore_errors=True)


# ── F2: 非 grounding 片段标注截断 + 归档回读 ─────────────────────────────

def test_non_grounding_fragment_marked_truncated():
    raw = "HEAD\n" + ("m" * 2000) + "\nMID_MARKER_X\n" + ("t" * 2000)
    state = _State([_tr(0, "some_unknown_tool", raw)])
    ledger, _text, _full = _run(_build_audit_evidence(state, budget=100000))
    assert "截断" in ledger, "非 grounding 片段必须标注截断（否则绕过硬性约束）"
    assert "非grounding" in ledger


def test_non_grounding_archive_readback_feeds_full_corpus():
    # 存档目录必须是持久化根（workspace/tool_results），否则按安全 allow-list 拒绝
    ws, cleanup = _make_ws()
    try:
        adir = ws / "tool_results"
        adir.mkdir()
        archive = adir / "arch.txt"
        archive.write_text(
            "HEAD\n" + ("x" * 2000) + "\nMID_ARCHIVE_MARKER_ZZZ\n" + ("y" * 2000),
            encoding="utf-8",
        )
        raw = (
            "<tool-digest>\nSummary省略\n"
            f"【全文存档】{archive}\n</tool-digest>"
        )
        state = _State([_tr(0, "some_unknown_tool", raw)])
        _ledger, _text, full = _run(_build_audit_evidence(state, budget=100000))
        assert "MID_ARCHIVE_MARKER_ZZZ" in full, (
            "非 grounding 结果有归档指针时，确定性全量语料必须回读全文"
        )
    finally:
        cleanup(ws, ignore_errors=True)


def test_archive_readback_rejects_non_persistence_path(tmp_path):
    """A4.9 r1 Security：工具输出里的任意路径不得让后端读进审计语料。"""
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP_SECRET_MARKER", encoding="utf-8")
    raw = f"<tool-digest>\n【全文存档】{secret}\n</tool-digest>\n" + ("z" * 1000)
    state = _State([_tr(0, "some_unknown_tool", raw)])
    _ledger, _text, full = _run(_build_audit_evidence(state, budget=100000))
    assert "TOP_SECRET_MARKER" not in full


def test_archive_readback_failure_not_overclaimed(tmp_path):
    """A4.9 r1 Important-1：回读失败不得标注「全文已回读」。"""
    adir = tmp_path / "tool_results"
    adir.mkdir()
    missing = adir / "gone.txt"  # 指针存在但文件不存在
    raw = f"<tool-digest>\n【全文存档】{missing}\n</tool-digest>\n" + ("z" * 1000)
    state = _State([_tr(0, "some_unknown_tool", raw)])
    ledger, _text, _full = _run(_build_audit_evidence(state, budget=100000))
    assert "全文已回读" not in ledger, "回读失败被过度声称成功"
    assert "回读未成功" in ledger


# ── F3: 死磕 judge 头+尾 ─────────────────────────────────────────────────

def test_head_tail_truncate_keeps_both_ends():
    from app.services.deathmatch_service import _head_tail_truncate
    text = "HEAD_MARK\n" + ("m" * 5000) + "\nTAIL_MARK"
    out = _head_tail_truncate(text, 1000)
    assert "HEAD_MARK" in out and "TAIL_MARK" in out
    assert len(out) <= 1000 + 40
    assert "中间省略" in out


def test_head_tail_truncate_tiny_limit_does_not_return_full_text():
    """A4.9 r2 Minor：limit ≤ 33 时 half=0 曾导致 text[-0:]=全文（假截断）。"""
    from app.services.deathmatch_service import _head_tail_truncate
    text = "x" * 1004
    for limit in (0, 10, 33):
        out = _head_tail_truncate(text, limit)
        assert len(out) < len(text), f"limit={limit} 不得返回全文"
        assert "截断" in out


def test_judge_evidence_head_tail_keeps_tail(tmp_path):
    from app.services.deathmatch_service import DeathmatchManager
    ws = Path(tmp_path)
    # 每个文件独立目录 → 列表不被 40/目录 聚合压缩，稳定超过 3000 字符预算
    for i in range(300):
        d = ws / f"d{i:03d}"
        d.mkdir()
        (d / "f.md").write_text("x" * 100, encoding="utf-8")
    conv = SimpleNamespace(
        id="conv-integrity",
        deathmatch_goal="目标",
        deathmatch_plan={"steps": []},
        deathmatch_last_verification_result={
            "status": "partial", "issues": ["TAIL_MARKER_ABC"], "workspace_files": [],
        },
        deathmatch_settled_ledger=None,
        deathmatch_context_summary=None,
    )
    mgr = DeathmatchManager(conv)
    ev = mgr._build_judge_evidence(str(ws))
    assert len(ev) <= 3000
    assert "TAIL_MARKER_ABC" in ev, "证据包尾部（上轮验证/工具轨迹）不得被头截断丢掉"


# ── F4: 死磕 verifier 显式 expected_output + 截断标注 ────────────────────

def _verifier_conv():
    return SimpleNamespace(
        id="conv-integrity-v",
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
             "expected_output": "E" * 900, "output_files": ["a.md"], "boundary": ""},
        ]},
        deathmatch_plan_version=1,
        deathmatch_reflections=[],
        deathmatch_last_verification_result={
            "status": "partial", "workspace_files": [], "completed_steps": [],
        },
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


def test_verifier_prompt_has_explicit_expected_output_and_markers(tmp_path):
    import app.services.deathmatch_service as ds
    from app.services.deathmatch_service import DeathmatchManager
    ds._CREATIVE_GOAL_CACHE[ds._normalize_goal_key("测试目标：产出一份报告")] = False
    ws = str(tmp_path)
    (Path(ws) / "a.md").write_text("content", encoding="utf-8")
    conv = _verifier_conv()
    mgr = DeathmatchManager(conv)
    captured = {}

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        captured["prompt"] = user_prompt
        return json.dumps({
            "status": "partial", "issues": [], "retry_instruction": "",
            "confidence": 0.5, "diagnosis": "x", "requires_file": True,
            "continuity_brief": "",
        }, ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    _run(mgr.verify_step_outputs("已产出", ws))
    prompt = captured.get("prompt") or ""
    assert "预期产出" in prompt, "步骤契约（expected_output）必须显式进入 verifier"
    assert "步骤 JSON 截断" in prompt, "超长步骤 JSON 截断必须显式标注"
    assert "E" * 100 in prompt, "expected_output 内容必须可见（非静默丢弃）"


# ── F5: 工作区快照上限披露 ────────────────────────────────────────────────

def test_workspace_snapshot_discloses_total_beyond_cap(tmp_path):
    from app.services.deathmatch_service import DeathmatchManager
    ws = Path(tmp_path)
    for i in range(405):
        (ws / f"file_{i:03d}.md").write_text("x", encoding="utf-8")
    conv = SimpleNamespace(id="c", deathmatch_plan={"steps": []})
    mgr = DeathmatchManager(conv)
    files = mgr._workspace_file_snapshot(str(ws))
    listing = mgr._format_workspace_listing(files, total=mgr._last_snapshot_total)
    assert len(files) == 400
    assert "共 405 个文件" in listing
    assert "未列出" in listing, "超上限必须显式披露（未列出 ≠ 不存在）"


# ── F6: 记忆注入截断标注 ─────────────────────────────────────────────────

def test_clip_marked_helper():
    from app.services.memory_retrieval_service import _clip_marked
    assert _clip_marked("short", 10) == "short"
    assert _clip_marked("a" * 20, 5) == "aaaaa…"


def test_memory_injection_uses_marked_clip_source_guard():
    import app.services.memory_retrieval_service as mrs
    src = open(mrs.__file__, encoding="utf-8").read()
    for banned in ("content[:200]", "content[:256]", "dream_text[:500]"):
        assert banned not in src, f"记忆注入不得再静默截断: {banned}"
    assert "_clip_marked" in src


# ── F8: 审计历史窗截断披露 ────────────────────────────────────────────────

def test_audit_history_window_discloses_omitted_count():
    messages = [{"role": "assistant", "content": "draft-so-far"}]
    # 2026-09-25（conv 1a24a04a 类别分窗）：terminal 属 grounding 类，
    # 历史窗口从统一 6 条改为 grounding 20 条 —— 23 条历史工具中
    # state.messages[:-1] 排除尾部草稿位 → 22 条候选纳入 20、省略 2。
    for i in range(23):
        messages.append({
            "role": "tool",
            "name": "terminal",
            "tool_call_id": f"t{i}",
            "content": f"HIST_TOOL_{i} " + "z" * 300,
        })
    state = _State([], messages=messages)
    ledger, _text, _full = _run(_build_audit_evidence(state, budget=100000))
    assert "历史" in ledger and "截断" in ledger, "历史窗截断必须显式披露"
    assert "另有 2 条" in ledger, "必须披露被省略的历史工具结果条数（2 条）"


# ── r2: JSON 转义指针回读 + 存档失败保全 ────────────────────────────────

def test_terminal_archive_readable_by_audit_integration():
    """A4.9 r2 Important-1：terminal 的 JSON 转义指针必须能被审计回读集成命中
    （单测存档文件存在 ≠ 审计能读——必须端到端喂进 _build_audit_evidence）。"""
    ws, cleanup = _make_ws()
    try:
        (ws / "huge.txt").write_text("D" * 40000, encoding="utf-8")
        import app.tools.terminal as term
        out = _run(term.terminal(
            {"command": "cat huge.txt", "timeout": 30},
            workspace_path=str(ws),
        ))
        assert "Full output saved to:" in out
        tr = ToolCallResult(call_id="c-term", name="terminal", arguments={}, result=out, error=False)
        _ledger, _text, full = _run(_build_audit_evidence(_State([tr]), budget=100000))
        assert "D" * 40000 in full, (
            "审计确定性语料未回读到 terminal 存档全文（JSON 转义指针解析失败）"
        )
    finally:
        cleanup(ws, ignore_errors=True)


def test_persist_failure_preserves_content(tmp_path, monkeypatch):
    """A4.9 r2 Important-2：存档失败绝不丢内容（旧实现传阈值 0 时整段变提示）。"""
    import app.services.tool_result_budget as trb

    def _boom(path, content):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(trb, "_write_persisted_file", _boom)
    payload = "E" * 5000
    out = _run(trb.maybe_persist_tool_result(
        payload, tool_name="terminal", tool_use_id="t1",
        config=trb.BudgetConfig(max_result_size_chars=0), workspace_path=str(tmp_path),
    ))
    assert "E" * 5000 in out, "存档失败时内容被丢弃"
    assert "存档失败" in out


# ── 复审：verify / validator / judge 剩余截断（r3+ 用户复审）─────────────

def test_draft_never_windowed_full_coverage_contract():
    """2026-10-06 用户红线（conv ae9aa092）：审计草稿绝不允许截断（断章取义）。

    旧 `_draft_audit_view` 头+尾窗口（含 token 真界收缩版）整体废除：
    - 函数与 `_AUDIT_DRAFT_WINDOW_TOKENS` 常量删除；
    - 审计 prompt 中草稿永远逐字全文；
    - 超巨型草稿走 `_chunked_full_audit` 分块全覆盖（零省略）。
    """
    _dir = os.path.join(os.path.dirname(__file__), "..", "app", "services")
    src = "\n".join(
        open(os.path.join(_dir, name), encoding="utf-8").read()
        for name in ("agent_loop.py", "agent_loop_audit.py", "agent_loop_orchestrator.py")
    )
    assert "_draft_audit_view" not in src, "草稿窗口函数必须删除（截断审计=断章取义）"
    assert "_AUDIT_DRAFT_WINDOW_TOKENS" not in src
    assert "窗口为头+尾" not in src
    assert "_chunked_full_audit" in src, "超巨型草稿必须走分块全覆盖审计"
    assert "助手草稿回答（逐字全文）" in src


def test_auditor_template_has_draft_full_coverage_rule():
    _dir = os.path.join(os.path.dirname(__file__), "..", "app", "services")
    src = "\n".join(
        open(os.path.join(_dir, name), encoding="utf-8").read()
        for name in ("agent_loop.py", "agent_loop_audit.py", "agent_loop_orchestrator.py")
    )
    assert "草稿全文纪律" in src, "模板必须携带草稿全文/分块全覆盖纪律"
    assert "草稿窗口截断纪律" not in src, "旧窗口截断纪律必须移除"
    assert "_audit_tools[:8]" not in src, "工具名单不得再截到前 8 个"
    assert "unsupported_claims[:5]" not in src, "确定性闸门不得只处理前 5 条声称"
    assert "_truncate_marked(problem" in src, "审计意见注入主上下文前必须显式裁剪标注"


def test_grounding_archive_unreadable_marked_truncated(tmp_path):
    adir = tmp_path / "tool_results"
    adir.mkdir()
    missing = adir / "gone.txt"
    raw = ("<persisted-output>\nFull output saved to: " + str(missing)
           + "\nPreview:\n" + ("v" * 2000) + "\n</persisted-output>")
    state = _State([_tr("web_search", raw)]) if False else _State([
        ToolCallResult(call_id="c1", name="web_search", arguments={}, result=raw, error=False)
    ])
    ledger, _text, _full = _run(_build_audit_evidence(state, budget=100000))
    assert "完整" not in ledger or "存档" in ledger, "存档回读失败不得标完整"
    assert "截断" in ledger


def test_multi_pointer_archive_readback():
    ws, cleanup = _make_ws()
    try:
        adir = ws / "tool_results"
        adir.mkdir()
        a1 = adir / "s1.txt"; a2 = adir / "s2.txt"
        a1.write_text("HEAD\nMARK_STDOUT_111\n" + "x" * 500, encoding="utf-8")
        a2.write_text("HEAD\nMARK_STDERR_222\n" + "y" * 500, encoding="utf-8")
        raw = json.dumps({
            "stdout": f"<persisted-output>\nFull output saved to: {a1}\nPreview:\n...\n</persisted-output>",
            "stderr": f"<persisted-output>\nFull output saved to: {a2}\nPreview:\n...\n</persisted-output>",
        })
        state = _State([ToolCallResult(call_id="c2", name="terminal", arguments={}, result=raw, error=False)])
        _ledger, _text, full = _run(_build_audit_evidence(state, budget=100000))
        assert "MARK_STDOUT_111" in full and "MARK_STDERR_222" in full, (
            "多指针存档必须全部回读（stdout/stderr 双流）"
        )
    finally:
        cleanup(ws, ignore_errors=True)


def test_partial_archive_readback_not_marked_complete():
    """A4.9 r4 Important-2：部分指针回读失败不得标「完整」，且内联/信封内容保留。"""
    ws, cleanup = _make_ws()
    try:
        adir = ws / "tool_results"
        adir.mkdir()
        ok = adir / "ok.txt"
        ok.write_text("MARK_OK_STREAM\n" + "x" * 500, encoding="utf-8")
        missing = adir / "gone.txt"
        raw = json.dumps({
            "stdout": f"<persisted-output>\nFull output saved to: {ok}\nPreview:\n...\n</persisted-output>",
            "stderr": f"INLINE_STREAM_MARKER <persisted-output>\nFull output saved to: {missing}\n</persisted-output>",
        })
        state = _State([ToolCallResult(call_id="c3", name="terminal", arguments={}, result=raw, error=False)])
        ledger, _text, full = _run(_build_audit_evidence(state, budget=100000))
        assert "MARK_OK_STREAM" in full
        assert "INLINE_STREAM_MARKER" in full, "内联（未存档）流内容不得因回读另一条存档而丢失"
        assert "截断(存档部分回读失败 1/2" in ledger
        assert "完整(存档全文已回读)" not in ledger
    finally:
        cleanup(ws, ignore_errors=True)


def test_audit_reserve_covers_draft_window_and_components():
    """A4.9 r4 Important-1：预留金必须覆盖实测非证据组件（含 8k 草稿窗口）。"""
    import app.services.agent_loop as al
    assert al._AUDIT_NON_EVIDENCE_RESERVE_TOKENS >= 30000
    ceiling = al.config.agent_audit_evidence_context_ceiling_tokens
    ev_budget = al._audit_evidence_budget()
    assert ev_budget + al._AUDIT_NON_EVIDENCE_RESERVE_TOKENS <= ceiling


def test_excerpt_discloses_unread_files(tmp_path):
    from app.services.deathmatch_service import _read_deliverable_excerpt
    ws = Path(tmp_path)
    files = []
    for i in range(13):
        p = f"f{i:02d}.md"
        (ws / p).write_text(("受限估计：待执行" if i == 12 else "clean"), encoding="utf-8")
        files.append(p)
    out = _read_deliverable_excerpt(str(ws), files)
    assert "未含在本摘录中" in out, "摘录文件上限必须显式披露（第 13 个未含）"


def test_prior_snippets_disclose_file_cap(tmp_path):
    from app.services.deathmatch_service import DeathmatchManager
    ws = Path(tmp_path)
    for i in range(5):
        (ws / f"out{i}.md").write_text(f"content {i}", encoding="utf-8")
    conv = SimpleNamespace(id="c", deathmatch_plan={"steps": []})
    mgr = DeathmatchManager(conv)
    steps = [{"id": "s1", "status": "done",
              "output_files": [f"out{i}.md" for i in range(5)]}]
    out = mgr._read_prior_file_snippets(steps, str(ws))
    assert "共 5 个" in out and "未展示" in out


def test_judge_done_steps_disclosure(tmp_path):
    from app.services.deathmatch_service import DeathmatchManager
    ws = Path(tmp_path)
    (ws / "p.md").write_text("x", encoding="utf-8")
    steps = [{"id": f"s{i}", "status": "done", "description": "d",
              "output_files": ["p.md"], "output_summary": "s"} for i in range(9)]
    conv = SimpleNamespace(
        id="c", deathmatch_goal="g", deathmatch_plan={"steps": steps},
        deathmatch_last_verification_result=None,
        deathmatch_settled_ledger=None, deathmatch_context_summary=None,
    )
    mgr = DeathmatchManager(conv)
    ev = mgr._build_judge_evidence(str(ws))
    assert "仅列前 8 个" in ev or "另有 1 个" in ev, "done 步骤上限必须披露"


# ── r5: 草稿窗口真界 + 预留金真界 + 发送前守卫 + 部分回读标注 ─────────────

def test_draft_full_text_regardless_of_mixed_content():
    """2026-10-06 用户红线：CJK/ASCII 混合长草稿不再做任何窗口测量收缩——
    审计输入恒为逐字全文（行为级断言在 test_audit_full_draft.py）。"""
    _dir = os.path.join(os.path.dirname(__file__), "..", "app", "services")
    src = "\n".join(
        open(os.path.join(_dir, name), encoding="utf-8").read()
        for name in ("agent_loop.py", "agent_loop_audit.py", "agent_loop_orchestrator.py")
    )
    assert "上下文上限：草稿窗口进一步缩小" not in src, "守卫不得再缩小草稿窗口"
    assert "先收起证据正文" in src or "证据正文因上下文上限未展开" in src


def test_reserve_is_true_bound_and_guard_present():
    import app.services.agent_loop as al
    assert al._AUDIT_NON_EVIDENCE_RESERVE_TOKENS >= 50000
    src = open(al.__file__, encoding="utf-8").read()
    audit_src = open(
        os.path.join(os.path.dirname(al.__file__), "agent_loop_audit.py"),
        encoding="utf-8").read()
    src += "\n" + audit_src
    assert "audit_prompt_guard" in src, "发送前硬守卫缺失"
    assert "另有 {len(_audit_tools) - 40} 次调用未列名" in src, "工具名上限必须披露余量"


def test_non_grounding_partial_readback_marked():
    """短非 grounding 结果（压缩展示分支）部分回读也不得声称全文。"""
    ws, cleanup = _make_ws()
    try:
        adir = ws / "tool_results"
        adir.mkdir()
        ok = adir / "ok.txt"
        ok.write_text("PARTIAL_MARK_OK\n" + "x" * 300, encoding="utf-8")
        missing = adir / "gone.txt"
        raw = (
            f"<tool-digest>SUMMARY\n【全文存档】{ok}\n"
            f"【全文存档】{missing}\n</tool-digest>"
        )
        # 长度 <800 → 非 grounding 走「压缩(非grounding…)」短结果分支
        state = _State([ToolCallResult(call_id="c9", name="some_unknown_tool",
                                       arguments={}, result=raw, error=False)])
        ledger, _text, _full = _run(_build_audit_evidence(state, budget=100000))
        assert "部分回读" in ledger, "短结果的部分回读必须显式标注"
    finally:
        cleanup(ws, ignore_errors=True)


# ── 残留清扫：workspace 收窄 allow-list / scout 部分回读 / enforce 强制存档 ──

def test_allowlist_scoped_to_workspace_when_provided():
    from app.services.agent_loop import _try_load_full_tool_results
    ws_a, cleanup_a = _make_ws()
    ws_b, cleanup_b = _make_ws()
    try:
        adir = ws_b / "tool_results"
        adir.mkdir()
        arch = adir / "x.txt"
        arch.write_text("OTHER_USER_SECRET_MARKER", encoding="utf-8")
        raw = f"<persisted-output>\nFull output saved to: {arch}\n</persisted-output>"
        # 无 workspace 上下文 → 全局根（兼容旧调用）
        txt, ok, total = _run(_try_load_full_tool_results(raw))
        assert ok == 1, "全局根下应可读（兼容路径）"
        # 指定 workspace=ws_a → ws_b 的归档不可读（跨用户收窄）
        txt2, ok2, total2 = _run(_try_load_full_tool_results(raw, str(ws_a)))
        assert ok2 == 0 and txt2 is None, "有 workspace 上下文时不得读取其他工作区归档"
    finally:
        cleanup_a(ws_a, ignore_errors=True)
        cleanup_b(ws_b, ignore_errors=True)


def test_build_scout_pack_partial_readback_labeled():
    import app.services.agent_loop as al
    from app.services.agent_loop import ToolCallResult, _build_scout_pack
    ws, cleanup = _make_ws()
    try:
        adir = ws / "tool_results"
        adir.mkdir()
        okf = adir / "ok.txt"
        okf.write_text("SCOUT_OK_MARK", encoding="utf-8")
        missing = adir / "gone.txt"
        raw = json.dumps({
            "stdout": f"<persisted-output>\nFull output saved to: {okf}\n</persisted-output>",
            "stderr": f"<persisted-output>\nFull output saved to: {missing}\n</persisted-output>",
        })
        tr = ToolCallResult(call_id="c", name="terminal", arguments={}, result=raw, error=False)
        pack = _run(_build_scout_pack([tr], [1], ["SCOUT"], budget_tokens=10000))
        assert "存档部分回读 1/2" in pack, "scout 部分回读必须标注"
    finally:
        cleanup(ws, ignore_errors=True)


def test_enforce_fallback_force_persists_with_pointer(tmp_path):
    import app.services.tool_result_budget as trb
    payload = "F" * 5000
    out = _run(trb.maybe_persist_tool_result(
        payload, tool_name="grep", tool_use_id="g1",
        config=trb.BudgetConfig(max_result_size_chars=10),
        workspace_path=str(tmp_path), force=True,
    ))
    assert "Full output saved to:" in out, "强制存档必须给出指针"
    archive = out.split("Full output saved to:")[1].splitlines()[0].strip()
    assert Path(archive).read_text(encoding="utf-8") == payload


def test_enforce_turn_budget_fallback_force_persists():
    """A4.9 r3 Minor：兜底阶段必须强制存档（含 _UNLIMITED_TOOLS），不得裸截断。"""
    import app.services.tool_result_budget as trb
    ws, cleanup = _make_ws()
    try:
        payload = "G" * 5000
        msgs = [{"role": "tool", "name": "grep", "tool_call_id": "g1", "content": payload}]
        cfg = trb.BudgetConfig(max_result_size_chars=100, turn_budget_chars=50, preview_chars=20)
        out = _run(trb.enforce_turn_budget(msgs, cfg, workspace_path=str(ws)))
        content = out[0]["content"]
        assert "Full output saved to:" in content, "兜底必须给出存档指针"
        archive = content.split("Full output saved to:")[1].splitlines()[0].strip()
        assert Path(archive).read_text(encoding="utf-8") == payload
    finally:
        cleanup(ws, ignore_errors=True)
