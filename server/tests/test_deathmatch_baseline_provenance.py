"""TDD — 遗留清偿波 fix-round C1/C2：A-N1 出处口径重建（phantom 红线）。

裁决（主会话逐条）：
- 基线名单在 generate_goal_plan（计划创建=目标环启动）捕获：os.walk 全量
  （不用 _workspace_file_snapshot 的 snap[:400] 截断），路径归一（反斜杠→斜杠
  + 去前缀 ./），排序，cap 5000；超 cap 记 plan["baseline_truncated"]=True；
  workspace 不可得（self._workspace_path 空）不写名单（未知=严格 mtime 回落）。
- replan/local_patch 继承 baseline_paths/baseline_truncated（同 created_at）。
- 豁免收窄：「"baseline_paths" in plan 且未 truncated 且归一化后非成员」才可
  豁免 mtime；空名单 [] 是合法「已知空基线」。
- verify_step_outputs 基线轮兜底捕获 = 全量 os.walk + plan["baseline_captured_late"]
  =True；late 名单不启用豁免（只记录观测）。
- lane1 声明路径比较前归一化（修 ./x 形态洞）。

判别测试（真实形状）：①>400 文件工作区 legacy 被拒（400 窗洞）②计划创建后
（含 turn-1）保 mtime 产出过闸（turn-1 误拒）③声明 ./deliverable.md 与基线
deliverable.md 匹配 ④合法空基线豁免成立 ⑤truncated 基线禁用豁免。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_deathmatch_baseline_provenance.py -v
"""
import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.deathmatch_service as ds
from app.services.deathmatch_service import DeathmatchManager


def _run(coro):
    return asyncio.run(coro)


def _conv(**over):
    plan = over.pop("plan", None)
    base = dict(
        id="conv-bp", deathmatch_mode=True, deathmatch_status="active",
        deathmatch_goal="测试目标", deathmatch_turns=1, deathmatch_max_turns=0,
        deathmatch_verify_failures=0, deathmatch_consecutive_failures=0,
        deathmatch_verdict=None, deathmatch_reason=None,
        deathmatch_plan=plan, deathmatch_plan_version=1,
        deathmatch_reflections=[], deathmatch_last_verification_result=None,
        deathmatch_wall_time_started_at=None, deathmatch_max_wall_time_seconds=0,
        deathmatch_wall_time_used_seconds=0, deathmatch_grilling_qa_history=[],
        deathmatch_grilling_complete=True, deathmatch_grilling_completed=0,
        deathmatch_grilling_total=0, deathmatch_grilling_round=0,
        deathmatch_grilling_round_total=3, deathmatch_human_gate=None,
        deathmatch_bible_draft=None, deathmatch_settled_ledger=None,
        deathmatch_subgoals=[], deathmatch_acceptance_criteria=None,
        deathmatch_failed_directions=None, deathmatch_pause_state=None,
        deathmatch_events=None, deathmatch_no_progress_replans=0,
        deathmatch_context_summary=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _mgr_verify(conv, parsed=None):
    """verify_step_outputs 的 verifier LLM 桩（生产判定形状：complete+requires_file）。"""
    import json as _json
    payload = {"status": "complete", "issues": [], "retry_instruction": "",
               "confidence": 0.9, "continuity_brief": "", "requires_file": True}
    if parsed:
        payload.update(parsed)
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return _json.dumps(payload, ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    mgr._record_settled = lambda *a, **k: None
    return mgr


def _mgr_plan(conv, plan):
    """generate_goal_plan 的规划器桩（LLM 面桩掉，捕获接线走真码）。"""
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return "{}"

    async def fake_parse(raw, user_prompt, llm, **kwargs):
        return {"steps": [dict(s) for s in plan.get("steps", [])]}

    async def fake_audit(p):
        return []

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    mgr._parse_plan_with_repair = fake_parse
    mgr._audit_plan_core = fake_audit
    mgr._build_plan_user_prompt = lambda: "prompt"
    mgr._self_eval_hint = lambda: ""
    mgr._apply_obligations = lambda **k: 0
    mgr._record_settled = lambda *a, **k: None
    mgr._record_event = lambda *a, **k: None
    return mgr


def _disable_obligation_gate(monkeypatch):
    monkeypatch.setitem(
        ds.config._config.setdefault("deathmatch", {}), "obligation_criteria_enabled", False)


def _preseed_creative_cache(monkeypatch):
    """禁 bible/LLM 副作用：目标恒判非创作（基线文件计数不被 bible 污染）。"""
    async def fake_creative(goal):
        return False
    monkeypatch.setattr(ds, "_ensure_creative_judged", fake_creative)


STEP = {"id": "s1", "status": "in_progress", "description": "d",
        "expected_output": "deliverable.md", "output_files": ["deliverable.md"]}
STEP_LEGACY = {"id": "s1", "status": "in_progress", "description": "d",
               "expected_output": "legacy.md", "output_files": ["legacy.md"]}


def _plan_at_creation(steps=None, **extra):
    p = {"steps": [dict(s) for s in (steps or [STEP])], "created_at": time.time() - 3600}
    p.update(extra)
    return p


# ── ①>400 文件工作区：目标启动前 legacy 被拒（修 400 窗洞） ──────────────

def test_plan_creation_captures_full_walk_not_snapshot_window(tmp_path, monkeypatch):
    """生成计划时基线名单=os.walk 全量（450 文件含最旧 legacy），不受 snap[:400] 截断。"""
    _disable_obligation_gate(monkeypatch)
    _preseed_creative_cache(monkeypatch)
    ws = tmp_path
    legacy = ws / "legacy.md"
    legacy.write_text("L" * 200, encoding="utf-8")
    old = time.time() - 86400
    os.utime(legacy, (old, old))
    for i in range(449):
        (ws / f"filler_{i:03d}.txt").write_text("x" * 20, encoding="utf-8")
    conv = _conv(plan=None)
    mgr = _mgr_plan(conv, {"steps": [dict(STEP)]})
    mgr._workspace_path = str(ws)
    plan = _run(mgr.generate_goal_plan(None))
    assert plan is not None
    captured = plan.get("baseline_paths") or []
    assert "legacy.md" in captured, "全量清单必须含最旧 legacy（400 窗洞修复）"
    assert len(captured) == 450, f"必须全量 450 个（snap[:400] 截断面禁止）: {len(captured)}"
    assert not plan.get("baseline_truncated"), "≤5000 不得标 truncated"


def test_goal_start_before_legacy_file_rejected_400_window(tmp_path, monkeypatch):
    """判别 ①：>400 文件工作区、目标启动前 legacy 文件在第二轮被拒。
    旧实现基线名单=snap[:400]（legacy 最旧滑出窗口）→ 误判「非成员」豁免放行。"""
    _preseed_creative_cache(monkeypatch)
    ws = tmp_path
    legacy = ws / "legacy.md"
    legacy.write_text("L" * 200, encoding="utf-8")
    old = time.time() - 86400
    os.utime(legacy, (old, old))
    for i in range(449):
        (ws / f"filler_{i:03d}.txt").write_text("x" * 20, encoding="utf-8")
    conv = _conv(plan=_plan_at_creation(steps=[dict(STEP_LEGACY)]))  # 存量计划：无 baseline_paths（未捕获）
    mgr = _mgr_verify(conv)
    # 首验 = 基线轮（捕获兜底名单，不判定）
    _run(mgr.verify_step_outputs("第一轮", str(ws)))
    assert conv.deathmatch_last_verification_result is not None
    # 二轮：声明 legacy.md（目标启动前遗留）——必须被拒
    _run(mgr.verify_step_outputs("已完成", str(ws)))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] != "done", "目标启动前 legacy 文件不得记账放行"


# ── ②计划创建后（含 turn-1）产出的保 mtime 文件过闸 ──────────────────────

def test_turn1_preserved_mtime_output_passes(tmp_path, monkeypatch):
    """判别 ②：unzip/tar 保 mtime 的目标内产出（计划创建后产出，含 turn-1）
    必须过闸。旧实现基线名单在 verify 首验（turn-1 产出之后）才捕获 → 产出
    被记成基线成员 → 保 mtime 被严格 mtime 误拒。"""
    _disable_obligation_gate(monkeypatch)
    _preseed_creative_cache(monkeypatch)
    ws = tmp_path
    (ws / "old.txt").write_text("x" * 200, encoding="utf-8")
    conv = _conv(plan=None)
    mgr = _mgr_plan(conv, {"steps": [dict(STEP)]})
    mgr._workspace_path = str(ws)
    plan = _run(mgr.generate_goal_plan(None))
    assert plan.get("baseline_paths") == ["old.txt"], "计划创建时基线=仅 old.txt"
    # turn-1 产出：归档解压保 mtime（历史时间戳）
    fp = ws / "deliverable.md"
    fp.write_text("L" * 200, encoding="utf-8")
    old = time.time() - 86400
    os.utime(fp, (old, old))
    vmgr = _mgr_verify(conv)  # 判定轮走 verifier 桩
    # 首验=基线轮
    _run(vmgr.verify_step_outputs("第一轮", str(ws)))
    # 二轮判定：保 mtime 产出不在基线名单 → 豁免 mtime → 过闸
    _run(vmgr.verify_step_outputs("已完成", str(ws)))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] == "done", "计划创建后（含 turn-1）保 mtime 产出必须过闸"


# ── ③声明 ./x 形态与基线 x 役配（归一化） ────────────────────────────────

def test_declared_dot_slash_form_matches_baseline_member(tmp_path):
    """判别 ③：声明 "./deliverable.md"、基线成员 "deliverable.md" 是同一路径——
    归一化后成员命中 → 不得豁免 → 目标启动前遗留被拒（修 ./x 形态洞）。"""
    ws = tmp_path
    fp = ws / "deliverable.md"
    fp.write_text("L" * 200, encoding="utf-8")
    old = time.time() - 86400
    os.utime(fp, (old, old))
    plan = _plan_at_creation(
        steps=[dict(STEP, output_files=["./deliverable.md"])],
        baseline_paths=["deliverable.md"],
    )
    conv = _conv(plan=plan)
    conv.deathmatch_last_verification_result = {
        "status": "partial", "workspace_files": [
            {"path": "deliverable.md", "size": 200, "ext": ".md", "mtime": int(old)}],
        "completed_steps": []}
    mgr = _mgr_verify(conv)
    _run(mgr.verify_step_outputs("已完成", str(ws)))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] != "done", (
        "声明 ./deliverable.md 必须归一化匹配基线 deliverable.md（遗留不得放行）")


# ── ④合法空基线（计划创建时工作区为空）豁免成立 ──────────────────────────

def test_empty_baseline_at_creation_is_known_and_exempts(tmp_path, monkeypatch):
    """判别 ④：计划创建时工作区为空 → baseline_paths=[]（合法「已知空基线」），
    此后任何产出（含保 mtime）都不是基线成员 → 豁免成立。"""
    _disable_obligation_gate(monkeypatch)
    _preseed_creative_cache(monkeypatch)
    ws = tmp_path
    conv = _conv(plan=None)
    mgr = _mgr_plan(conv, {"steps": [dict(STEP)]})
    mgr._workspace_path = str(ws)
    plan = _run(mgr.generate_goal_plan(None))
    assert plan.get("baseline_paths") == [], "空工作区 = 已知空基线（[] 保留，不写 late）"
    # 目标内产出（保 mtime）
    fp = ws / "deliverable.md"
    fp.write_text("L" * 200, encoding="utf-8")
    old = time.time() - 86400
    os.utime(fp, (old, old))
    vmgr = _mgr_verify(conv)  # 判定轮走 verifier 桩
    _run(vmgr.verify_step_outputs("第一轮", str(ws)))
    _run(vmgr.verify_step_outputs("已完成", str(ws)))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] == "done", "已知空基线：目标内产出（非成员）豁免必须成立"


# ── ⑤truncated 基线禁用豁免 ─────────────────────────────────────────────

def test_truncated_baseline_disables_exemption(tmp_path):
    """判别 ⑤：基线名单超 cap 截断 → 名单不完整 → 禁用豁免（未知=严格 mtime）。
    名单外的 legacy 文件不得借「非成员」放行。"""
    ws = tmp_path
    fp = ws / "legacy.md"
    fp.write_text("L" * 200, encoding="utf-8")
    old = time.time() - 86400
    os.utime(fp, (old, old))
    filler = [f"f{i}.txt" for i in range(5000)]
    plan = _plan_at_creation(
        steps=[dict(STEP_LEGACY)],
        baseline_paths=filler, baseline_truncated=True,
    )
    conv = _conv(plan=plan)
    conv.deathmatch_last_verification_result = {
        "status": "partial", "workspace_files": [
            {"path": "legacy.md", "size": 200, "ext": ".md", "mtime": int(old)}],
        "completed_steps": []}
    mgr = _mgr_verify(conv)
    _run(mgr.verify_step_outputs("已完成", str(ws)))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] != "done", "truncated 基线必须禁用豁免（legacy 不得放行）"


# ── 兜底捕获：全量 + late 标记（late 名单不启用豁免） ─────────────────────

def test_baseline_round_fallback_full_walk_marks_late(tmp_path, monkeypatch):
    """verify 基线轮兜底捕获 = os.walk 全量 + baseline_captured_late=True；
    late 名单只记录观测（豁免不启用——即便成员判定成立）。"""
    _preseed_creative_cache(monkeypatch)
    ws = tmp_path
    fp = ws / "legacy.md"
    fp.write_text("L" * 200, encoding="utf-8")
    old = time.time() - 86400
    os.utime(fp, (old, old))
    for i in range(10):
        (ws / f"n{i}.txt").write_text("x" * 20, encoding="utf-8")
    conv = _conv(plan=_plan_at_creation(steps=[dict(STEP_LEGACY)]))
    mgr = _mgr_verify(conv)
    _run(mgr.verify_step_outputs("第一轮", str(ws)))
    plan = conv.deathmatch_plan
    assert plan.get("baseline_captured_late") is True, "兜底捕获必须标 late"
    assert "legacy.md" in (plan.get("baseline_paths") or []), "兜底捕获必须全量"
    assert len(plan.get("baseline_paths") or []) == 11
    # late 名单不启用豁免：老 mtime 的 legacy 仍被严格口径拒
    _run(mgr.verify_step_outputs("已完成", str(ws)))
    assert plan["steps"][0]["status"] != "done", "late 名单不得启用豁免"


def test_workspace_unavailable_at_plan_creation_writes_no_list(tmp_path, monkeypatch):
    """workspace 不可得（_workspace_path 空）→ 不写名单（未知=严格 mtime 回落）。"""
    _disable_obligation_gate(monkeypatch)
    conv = _conv(plan=None)
    mgr = _mgr_plan(conv, {"steps": [dict(STEP)]})
    mgr._workspace_path = ""
    plan = _run(mgr.generate_goal_plan(None))
    assert plan is not None
    assert "baseline_paths" not in plan, "workspace 不可得不得写名单"
    assert "baseline_truncated" not in plan


def test_local_patch_inherits_baseline_anchors():
    """replan/local_patch 继承 baseline_paths/baseline_truncated（同 created_at）。"""
    import json as _json
    plan = _plan_at_creation(baseline_paths=["old.txt"], baseline_truncated=False)
    conv = _conv(plan=plan)
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return _json.dumps({"steps": [{
            "id": "s1", "description": "重写", "expected_output": "a.md"}]},
            ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen

    async def fake_judge(steps, **k):
        return None

    mgr._judge_steps_needing_evidence_spec = fake_judge
    ok = _run(mgr._local_patch_step(conv.deathmatch_plan["steps"][0], ["问题"]))
    assert ok
    patched = conv.deathmatch_plan
    assert patched.get("baseline_paths") == ["old.txt"], "补丁必须继承基线名单"
    assert patched.get("baseline_truncated") is False
    assert patched.get("created_at") == plan["created_at"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_turn1_baseline_captured_via_workspace_resolver(tmp_path, monkeypatch):
    """scoped 复审必修：计划创建时 _workspace_path 恒空（生产形状）——
    基线捕获必须经 ensure_user_workspace 解析真实工作区，不得死接线。"""
    _disable_obligation_gate(monkeypatch)
    _preseed_creative_cache(monkeypatch)
    ws = tmp_path
    (ws / "legacy.md").write_text("L" * 200, encoding="utf-8")
    conv = _conv(plan=None, user_id="u1")
    mgr = _mgr_plan(conv, {"steps": [dict(STEP)]})
    mgr._workspace_path = ""  # ← 生产形状：计划创建时未赋值

    class _WS:
        root_path = str(ws)

    async def fake_ensure(db, user_id, username=None):
        return _WS()

    import app.services.workspace_service as wsm
    monkeypatch.setattr(wsm, "ensure_user_workspace", fake_ensure)
    plan = _run(mgr.generate_goal_plan(None))
    assert plan is not None
    assert "legacy.md" in (plan.get("baseline_paths") or []), (
        "生产形状（_workspace_path 空）必须经解析器捕获基线名单")
    assert not plan.get("baseline_captured_late"), "计划创建捕获不得标 late"


def test_turn1_preserved_mtime_output_passes_via_resolver(tmp_path, monkeypatch):
    """端到端（生产形状）：计划创建捕获基线 → turn-1 保 mtime 产出过闸。"""
    _preseed_creative_cache(monkeypatch)
    ws = tmp_path
    conv = _conv(plan=None, user_id="u1")
    mgr_plan = _mgr_plan(conv, {"steps": [dict(STEP)]})
    mgr_plan._workspace_path = ""

    class _WS:
        root_path = str(ws)

    async def fake_ensure(db, user_id, username=None):
        return _WS()

    import app.services.workspace_service as wsm
    monkeypatch.setattr(wsm, "ensure_user_workspace", fake_ensure)
    plan = _run(mgr_plan.generate_goal_plan(None))
    assert plan and "baseline_paths" in plan
    conv.deathmatch_plan = plan
    # turn-1：agent 解压产出（保留历史 mtime）
    fp = ws / "deliverable.md"
    fp.write_text("L" * 200, encoding="utf-8")
    old = time.time() - 86400
    os.utime(fp, (old, old))
    conv.deathmatch_last_verification_result = {
        "status": "partial", "workspace_files": [
            {"path": "deliverable.md", "size": 200, "ext": ".md", "mtime": int(old)}],
        "completed_steps": []}
    mgr = _mgr_verify(conv)
    _run(mgr.verify_step_outputs("已完成", str(ws)))
    assert conv.deathmatch_plan["steps"][0]["status"] == "done", (
        "计划创建后产出（保 mtime）必须过闸——turn-1 误拒面端到端修复）")


def test_normalize_rel_path_collapses_inner_dots():
    """scoped 复审 NEW-1：sub/./file.md ≡ sub/file.md（normpath 级归一）。"""
    from app.services.deathmatch_service import _normalize_rel_path
    assert _normalize_rel_path("sub/./file.md") == "sub/file.md"
    assert _normalize_rel_path("././x.md") == "x.md"
    assert _normalize_rel_path("sub//file.md") == "sub/file.md"
    assert _normalize_rel_path("a\\b.md") == "a/b.md"
