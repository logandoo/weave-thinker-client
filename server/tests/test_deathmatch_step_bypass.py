"""TDD — R1 证据闸三车道 + R2 卡步旁路（诊断 2026-10-13 机制 A）。

机制 A：合成/改写型步骤被 A1a「当轮新文件>100B」闸卡在 in_progress 后
垄断步骤机（只认 in_progress/首个 pending）→ 后续步骤永不激活/打勾。

R1 三车道（对『verifier 判 complete 但被证据闸拒』分诊）：
  lane 1 声明产出已存在（任意轮产出）→ done + step_done_on_existing_artifact
  lane 2 合成步（无声明产出/无 evidence_spec）连拦 2 次 → 放行 done + evidence_gate_override
  lane 3 证据步（声明产出/evidence_spec 持续缺失）→ 连拦 3 次 → R2 降级 pending+stuck + step_stuck_bypass
R2 选择旁路：stuck 步在有其他候选时被跳过，无候选时兜底重拾。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_deathmatch_step_bypass.py -v
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


def _conv(**over):
    base = dict(
        id="conv-bypass",
        deathmatch_mode=True,
        deathmatch_status="active",
        deathmatch_goal="测试目标",
        deathmatch_turns=3,
        deathmatch_max_turns=0,
        deathmatch_verify_failures=0,
        deathmatch_consecutive_failures=0,
        deathmatch_verdict=None,
        deathmatch_reason=None,
        deathmatch_plan={"steps": [
            {"id": "s1", "status": "in_progress", "description": "步骤1",
             "expected_output": "deliverable.md", "verification_method": "",
             "boundary": ""},
        ]},
        deathmatch_plan_version=1,
        deathmatch_reflections=[],
        deathmatch_last_verification_result=None,
        deathmatch_wall_time_started_at=None,
        deathmatch_max_wall_time_seconds=0,
        deathmatch_wall_time_used_seconds=0,
        deathmatch_grilling_qa_history=[],
        deathmatch_grilling_complete=True,
        deathmatch_grilling_completed=0,
        deathmatch_grilling_total=0,
        deathmatch_grilling_round=0,
        deathmatch_grilling_round_total=3,
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
    key = ds._normalize_goal_key("测试目标")
    ds._CREATIVE_GOAL_CACHE[key] = False
    yield
    ds._CREATIVE_GOAL_CACHE.pop(key, None)


def _run(coro):
    return asyncio.run(coro)


_COMPLETE = {"status": "complete", "issues": [], "retry_instruction": "",
             "confidence": 0.9, "continuity_brief": "", "requires_file": True}


def _mgr(conv, parsed=None):
    mgr = DeathmatchManager(conv)
    payload = dict(_COMPLETE)
    if parsed:
        payload.update(parsed)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return json.dumps(payload, ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    mgr._record_settled = lambda *a, **k: None
    return mgr


def _events(conv, etype):
    return [e for e in (conv.deathmatch_events or []) if e.get("type") == etype]


def _prev_snapshot(conv, ws, paths=None):
    """种入非基线轮：给定 paths（缺省=ws 全部文件）以本轮快照同 mtime 回填。"""
    files = []
    for rel in (paths if paths is not None else []):
        fp = os.path.join(ws, rel)
        st = os.stat(fp)
        files.append({"path": rel, "size": st.st_size, "ext": os.path.splitext(rel)[1], "mtime": int(st.st_mtime)})
    conv.deathmatch_last_verification_result = {
        "status": "partial", "workspace_files": files, "completed_steps": [],
    }


# ── 判据 2：lane 1 声明产出已存在（早轮产出）→ 记账放行 ──────────────────
def test_existing_declared_artifact_credits_completion(tmp_path):
    ws = str(tmp_path)
    (tmp_path / "chapter1.txt").write_text("正文内容。" * 50, encoding="utf-8")
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s2", "status": "in_progress", "description": "写第一章",
         "expected_output": "chapter1.txt", "output_files": ["chapter1.txt"]},
    ]})
    _prev_snapshot(conv, ws, ["chapter1.txt"])  # 本轮无新文件
    mgr = _mgr(conv)
    res = _run(mgr.verify_step_outputs("已完成第一章撰写", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] == "done", f"声明产出已存在应记账放行，issues={res.get('issues')}"
    assert _events(conv, "step_done_on_existing_artifact"), "缺 step_done_on_existing_artifact 事件"


def test_existing_declared_artifact_via_evidence_spec_receipt(tmp_path):
    ws = str(tmp_path)
    (tmp_path / "word_count.txt").write_text("count=1234\n" * 20, encoding="utf-8")
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s3", "status": "in_progress", "description": "统计字数",
         "expected_output": "字数统计", "evidence_spec": {
             "kind": "receipt", "must_run": True,
             "receipt": {"cmds": ["wc"], "outputs": ["word_count.txt"]}}},
    ]})
    _prev_snapshot(conv, ws, ["word_count.txt"])
    mgr = _mgr(conv)
    _run(mgr.verify_step_outputs("已统计", ws))
    assert conv.deathmatch_plan["steps"][0]["status"] == "done"


# ── 判据 3：lane 2 合成步连拦 2 次 → 放行需记录 ─────────────────────────
def test_synthetic_step_override_after_two_blocks(tmp_path):
    ws = str(tmp_path)
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "综合分析",
         "expected_output": "分析结论（对话内交付）"},
    ]})
    _prev_snapshot(conv, ws, [])
    mgr = _mgr(conv)
    _run(mgr.verify_step_outputs("分析结论：……", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] == "in_progress", "第 1 次拦截应保持重试"
    assert int(step.get("gate_blocks") or 0) == 1
    assert _events(conv, "evidence_gate_block")
    _run(mgr.verify_step_outputs("分析结论：……", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] == "done", "合成步连拦 2 次后应放行"
    assert _events(conv, "evidence_gate_override"), "放行必须留痕（evidence_gate_override）"
    assert not _events(conv, "step_done_on_existing_artifact")


def test_synthetic_step_override_records_no_settled(tmp_path):
    """放行是软完成：不入 settled 台账（新证据可再审）。"""
    ws = str(tmp_path)
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "综合分析",
         "expected_output": "分析结论"},
    ]})
    _prev_snapshot(conv, ws, [])
    settled = []
    mgr = _mgr(conv)
    mgr._record_settled = lambda entry, **k: settled.append(entry)
    _run(mgr.verify_step_outputs("结论", ws))
    _run(mgr.verify_step_outputs("结论", ws))
    assert conv.deathmatch_plan["steps"][0]["status"] == "done", "放行应已发生（先证放行再证不入账）"
    assert settled == [], "evidence_gate_override 不得入 settled"


# ── 判据 4：lane 3 证据步连拦 3 次 → 降级 pending+stuck + 旁路 ──────────
def test_critical_step_stuck_after_three_blocks_then_bypass(tmp_path):
    ws = str(tmp_path)
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "写交付物",
         "expected_output": "deliverable.md", "output_files": ["deliverable.md"]},
        {"id": "s2", "status": "pending", "description": "后续步骤",
         "expected_output": "next.md"},
    ]})
    _prev_snapshot(conv, ws, [])
    mgr = _mgr(conv)
    for i in (1, 2):
        _run(mgr.verify_step_outputs("已完成", ws))
        step = conv.deathmatch_plan["steps"][0]
        assert step["status"] == "in_progress", f"第 {i} 次拦截应保持 in_progress"
        assert int(step.get("gate_blocks") or 0) == i
    _run(mgr.verify_step_outputs("已完成", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] == "pending", "3 次拦截后应降级 pending"
    assert step.get("stuck") is True, "应标记可见 stuck"
    assert _events(conv, "step_stuck_bypass"), "缺 step_stuck_bypass 事件"
    assert mgr._get_next_pending_step()["id"] == "s2", "步骤机必须前移到 s2"


def test_evidence_spec_step_never_overrides(tmp_path):
    ws = str(tmp_path)
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "实证",
         "expected_output": "实证结果", "evidence_spec": {
             "kind": "receipt", "must_run": True,
             "receipt": {"cmds": ["run"], "outputs": ["empirical.md"]}}},
    ]})
    _prev_snapshot(conv, ws, [])
    mgr = _mgr(conv)
    _run(mgr.verify_step_outputs("已执行", ws))
    _run(mgr.verify_step_outputs("已执行", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] != "done", "evidence_spec 步不得走合成步放行"
    assert not _events(conv, "evidence_gate_override")


# ── 判据 5：stuck 选择旁路 ──────────────────────────────────────────────
def test_stuck_step_skipped_when_other_candidates_exist():
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "pending", "description": "卡步", "stuck": True},
        {"id": "s2", "status": "pending", "description": "可行动"},
    ]})
    mgr = DeathmatchManager(conv)
    picked = mgr._get_next_pending_step()
    assert picked is not None and picked["id"] == "s2"


def test_stuck_step_last_resort_when_alone():
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "pending", "description": "卡步", "stuck": True},
    ]})
    mgr = DeathmatchManager(conv)
    picked = mgr._get_next_pending_step()
    assert picked is not None and picked["id"] == "s1", "无其他候选时必须兜底重拾（无空转）"


def test_unstuck_on_done():
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "卡步", "stuck": True},
    ]})
    mgr = DeathmatchManager(conv)
    mgr._set_step_status(conv.deathmatch_plan["steps"][0], "done", "test")
    assert not conv.deathmatch_plan["steps"][0].get("stuck")


# ── 判据 6：迁移事件（pick 路径）─────────────────────────────────────────
def test_step_transition_events_recorded(tmp_path):
    ws = str(tmp_path)
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "pending", "description": "步骤1",
         "expected_output": "a.md"},
    ]})
    _prev_snapshot(conv, ws, [])
    mgr = _mgr(conv, {"status": "partial"})
    _run(mgr.verify_step_outputs("进行中", ws))
    transitions = _events(conv, "step_transition")
    assert transitions, "步骤状态迁移必须写 step_transition 事件（R5）"
    ev = transitions[-1]
    assert ev.get("step_id") == "s1"
    assert ev.get("from") == "pending"
    assert ev.get("to") == "in_progress"


# ── 判据 7 数据面：plan_steps 载荷带 stuck ──────────────────────────────
def test_plan_steps_payload_carries_stuck():
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "pending", "description": "卡步", "stuck": True},
        {"id": "s2", "status": "done", "description": "完成"},
    ]})
    mgr = DeathmatchManager(conv)
    steps = mgr.get_verdict_dict()["plan_steps"]
    assert steps[0]["stuck"] is True
    assert steps[1].get("stuck") in (False, None)


# ════════ fix-round 1（双对抗评审 A-C1/I1/I2/I3 + B-1/B-3/M2）判别测试 ════════

def _conv_with_created(**over):
    import time as _t
    plan = over.pop("deathmatch_plan", {"steps": [
        {"id": "s1", "status": "in_progress", "description": "步骤1",
         "expected_output": "deliverable.md", "output_files": ["deliverable.md"]},
    ]})
    plan = dict(plan)
    plan.setdefault("created_at", _t.time() - 3600)  # 目标环 1 小时前启动
    return _conv(deathmatch_plan=plan, **over)


def test_lane1_requires_all_declared_outputs(tmp_path):
    """A-C1：声明产出必须全部在场（ANY-match 会把多产出步骤假勾选）。"""
    ws = str(tmp_path)
    (tmp_path / "a.txt").write_text("A" * 200, encoding="utf-8")
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "双产出",
         "expected_output": "a.txt+b.txt", "output_files": ["a.txt", "b.txt"]},
    ], "created_at": __import__("time").time() - 3600})
    _prev_snapshot(conv, ws, ["a.txt"])
    mgr = _mgr(conv)
    _run(mgr.verify_step_outputs("已完成 a.txt", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] != "done", "缺 b.txt 不得记账放行"
    assert not _events(conv, "step_done_on_existing_artifact")


def test_lane1_rejects_legacy_stale_file(tmp_path):
    """A-C1/B-2：目标环启动前的同名遗留文件 = 无出处，不得记账（phantom 面）。"""
    import os as _os
    import time as _t
    ws = str(tmp_path)
    fp = tmp_path / "deliverable.md"
    fp.write_text("L" * 200, encoding="utf-8")
    old = _t.time() - 86400  # 一天前的遗留文件
    _os.utime(fp, (old, old))
    conv = _conv_with_created()
    _prev_snapshot(conv, ws, ["deliverable.md"])
    mgr = _mgr(conv)
    _run(mgr.verify_step_outputs("已完成", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] != "done", "目标启动前的遗留文件不得记账放行"
    assert not _events(conv, "step_done_on_existing_artifact")


def test_lane1_preserves_declared_output_files(tmp_path):
    """A-C1：记账放行不得覆写 output_files（丢声明=G3 保护面失明）。"""
    ws = str(tmp_path)
    (tmp_path / "a.txt").write_text("A" * 200, encoding="utf-8")
    (tmp_path / "b.txt").write_text("B" * 200, encoding="utf-8")
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "双产出",
         "expected_output": "a.txt+b.txt", "output_files": ["b.txt", "a.txt"]},
    ], "created_at": __import__("time").time() - 3600})
    _prev_snapshot(conv, ws, ["a.txt", "b.txt"])
    mgr = _mgr(conv)
    _run(mgr.verify_step_outputs("已完成", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] == "done"
    assert step.get("output_files") == ["b.txt", "a.txt"], "声明清单不得被覆写/重排为实存排序集"


def test_bypass_respects_dependencies():
    """I2：stuck 依赖步是唯一可行动工作——不得跳到依赖未满足的后续步。"""
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "pending", "description": "卡步", "stuck": True},
        {"id": "s2", "status": "pending", "description": "依赖步", "dependencies": ["s1"]},
    ]})
    mgr = DeathmatchManager(conv)
    picked = mgr._get_next_pending_step()
    assert picked is not None and picked["id"] == "s1", "依赖未满足时应重拾 stuck 依赖步"


def test_stuck_reentry_resets_gate_blocks():
    """I1/B-1：任何拾起点（pending→in_progress）都清零重试预算（D-12）。"""
    conv = _conv_with_created()
    step = conv.deathmatch_plan["steps"][0]
    step["status"] = "pending"
    step["stuck"] = True
    step["gate_blocks"] = 3
    mgr = DeathmatchManager(conv)
    mgr._set_step_status(step, "in_progress", "prompt_next_step")
    assert step["status"] == "in_progress"
    assert int(step.get("gate_blocks") or 0) == 0, "重拾必须给足新预算（清零 gate_blocks）"


def test_step_transition_bumps_plan_version():
    """I3：步骤迁移推进 plan_version（否则判据 8 的重播种条件在生产链路是死的）。"""
    conv = _conv_with_created(deathmatch_plan_version=7)
    mgr = DeathmatchManager(conv)
    mgr._set_step_status(conv.deathmatch_plan["steps"][0], "done", "test")
    assert int(conv.deathmatch_plan_version) == 8
    # 无实际迁移（同状态）不推进
    mgr._set_step_status(conv.deathmatch_plan["steps"][0], "done", "test")
    assert int(conv.deathmatch_plan_version) == 8


def test_verifier_partial_round_resets_gate_blocks_streak(tmp_path):
    """M2：gate_blocks 计「连续被拦」——中间的 partial 轮必须打断连击。"""
    ws = str(tmp_path)
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "综合分析",
         "expected_output": "分析结论"},
    ], "created_at": __import__("time").time() - 3600})
    _prev_snapshot(conv, ws, [])
    mgr = _mgr(conv, {"status": "complete"})
    _run(mgr.verify_step_outputs("结论", ws))
    assert int(conv.deathmatch_plan["steps"][0].get("gate_blocks") or 0) == 1
    mgr2 = _mgr(conv, {"status": "partial"})
    _run(mgr2.verify_step_outputs("继续中", ws))
    assert int(conv.deathmatch_plan["steps"][0].get("gate_blocks") or 0) == 0, "partial 轮打断连击"
    mgr3 = _mgr(conv, {"status": "complete"})
    _run(mgr3.verify_step_outputs("结论", ws))
    assert conv.deathmatch_plan["steps"][0]["status"] == "in_progress", "连击重计后仅 1 拦不得放行"


def test_lane1_existence_via_filesystem_not_snapshot(tmp_path):
    """A-I4：存在性必须走文件系统（snap[:400] 截断快照会误判窗口外声明产出缺失）。"""
    ws = str(tmp_path)
    (tmp_path / "deliverable.md").write_text("F" * 200, encoding="utf-8")
    conv = _conv_with_created()
    _prev_snapshot(conv, ws, ["deliverable.md"])
    mgr = _mgr(conv)
    mgr._workspace_file_snapshot = lambda _ws: []  # 模拟快照截断/丢失
    _run(mgr.verify_step_outputs("已完成", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] == "done", "快照不可见但文件系统存在的声明产出应记账放行"
    assert _events(conv, "step_done_on_existing_artifact")


def test_lane1_rejects_out_of_workspace_declared_path(tmp_path):
    """N1（复审）：声明绝对路径/.. 不得经 join 逃出工作区记账（安全面）。"""
    import os as _os
    ws = str(tmp_path)
    outside = tmp_path.parent / "outside_deliverable.md"
    outside.write_text("X" * 200, encoding="utf-8")
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "越界声明",
         "expected_output": "x", "output_files": [str(outside)]},
    ], "created_at": __import__("time").time() - 3600})
    _prev_snapshot(conv, ws, [])
    mgr = _mgr(conv)
    _run(mgr.verify_step_outputs("已完成", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] != "done", "绝对路径声明产出不得记账放行"
    assert not _events(conv, "step_done_on_existing_artifact")
    _os.unlink(outside)


def test_lane1_legacy_plan_without_created_at_transitional(tmp_path):
    """N3/D-14（复审）：存量计划无 created_at 锚——过渡口径允许记账（ALL-match 仍全量）。"""
    ws = str(tmp_path)
    (tmp_path / "deliverable.md").write_text("L" * 200, encoding="utf-8")
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "存量计划步",
         "expected_output": "deliverable.md", "output_files": ["deliverable.md"]},
    ]})  # 无 created_at
    _prev_snapshot(conv, ws, ["deliverable.md"])
    mgr = _mgr(conv)
    _run(mgr.verify_step_outputs("已完成", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] == "done", "存量计划（无锚）应走过渡口径记账"
    assert _events(conv, "step_done_on_existing_artifact")


def test_local_patch_records_transition_and_preserves_stuck():
    """B-3/M1（复审）：local_patch 替换写点须留迁移事件并保留 stuck/gate_blocks。"""
    import json as _json
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "原步骤",
         "expected_output": "a.md", "stuck": True, "gate_blocks": 3},
    ]})
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return _json.dumps({"steps": [{
            "id": "s1", "description": "重写后步骤", "expected_output": "a.md",
        }]}, ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    mgr._judge_steps_needing_evidence_spec = lambda steps, **k: __import__("asyncio").sleep(0, result=None)
    ok = _run(mgr._local_patch_step(conv.deathmatch_plan["steps"][0], ["问题1"]))
    assert ok, "local_patch 应成功应用"
    step = conv.deathmatch_plan["steps"][0]
    assert step.get("stuck") is True, "stuck 标记必须保留"
    assert int(step.get("gate_blocks") or 0) == 3, "gate_blocks 必须保留"
    transitions = _events(conv, "step_transition")
    assert transitions and transitions[-1].get("cause") == "local_patch", "替换迁移必须留事件"


def test_new_small_artifact_credits_completion(tmp_path):
    """A valid 17-byte deliverable must not be rejected by an arbitrary 100B floor."""
    ws = str(tmp_path)
    payload = b"ALPHA\nBETA\nGAMMA\n"
    assert len(payload) == 17
    (tmp_path / "dm_feedback_t1.txt").write_bytes(payload)
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "写三行文本",
         "expected_output": "dm_feedback_t1.txt", "output_files": ["dm_feedback_t1.txt"]},
    ]})
    _prev_snapshot(conv, ws, [])
    mgr = _mgr(conv)
    res = _run(mgr.verify_step_outputs("已写入并完整回读三行文本", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] == "done", f"17B 真实非空交付物应记完成，issues={res.get('issues')}"


def test_existing_small_declared_artifact_credits_completion(tmp_path):
    """The same 17-byte artifact must also pass the existing-artifact lane."""
    ws = str(tmp_path)
    payload = b"ALPHA\nBETA\nGAMMA\n"
    (tmp_path / "dm_feedback_t1.txt").write_bytes(payload)
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "写三行文本",
         "expected_output": "dm_feedback_t1.txt", "output_files": ["dm_feedback_t1.txt"]},
    ]})
    _prev_snapshot(conv, ws, ["dm_feedback_t1.txt"])
    mgr = _mgr(conv)
    res = _run(mgr.verify_step_outputs("已完成并核对", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] == "done", f"17B 早轮声明产出应记账放行，issues={res.get('issues')}"
    assert _events(conv, "step_done_on_existing_artifact")


def test_unrelated_artifact_does_not_satisfy_declared_output(tmp_path):
    ws = str(tmp_path)
    (tmp_path / "unrelated.tmp").write_text("x" * 200, encoding="utf-8")
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "写指定交付物",
         "expected_output": "required.txt", "output_files": ["required.txt"]},
    ]})
    _prev_snapshot(conv, ws, [])
    mgr = _mgr(conv)
    res = _run(mgr.verify_step_outputs("只生成了无关文件", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] != "done", f"无关文件不得替代 required.txt，issues={res.get('issues')}"


def test_empty_declared_artifact_is_not_evidence(tmp_path):
    ws = str(tmp_path)
    (tmp_path / "required.txt").write_bytes(b"")
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "写指定交付物",
         "expected_output": "required.txt", "output_files": ["required.txt"]},
    ]})
    _prev_snapshot(conv, ws, [])
    mgr = _mgr(conv)
    res = _run(mgr.verify_step_outputs("只有空文件", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] != "done", f"空声明文件仍不得冒充交付物，issues={res.get('issues')}"


def test_partial_declared_outputs_do_not_complete_step(tmp_path):
    ws = str(tmp_path)
    (tmp_path / "a.txt").write_text("a" * 20, encoding="utf-8")
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "双文件交付",
         "expected_output": "a.txt 与 b.txt", "output_files": ["a.txt", "b.txt"]},
    ]})
    _prev_snapshot(conv, ws, [])
    mgr = _mgr(conv)
    res = _run(mgr.verify_step_outputs("只完成 a.txt", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] != "done", f"声明产出必须全部存在，issues={res.get('issues')}"


def test_requires_file_false_does_not_bypass_declared_outputs(tmp_path):
    ws = str(tmp_path)
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "显式文件义务",
         "expected_output": "required.txt", "output_files": ["required.txt"]},
    ]})
    _prev_snapshot(conv, ws, [])
    mgr = _mgr(conv, {"requires_file": False})
    res = _run(mgr.verify_step_outputs("verifier 错误声称不需要文件", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] != "done", f"显式声明优先于 requires_file=false，issues={res.get('issues')}"


def test_requires_file_false_synthetic_step_can_complete(tmp_path):
    ws = str(tmp_path)
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "纯分析回答",
         "expected_output": "对话内分析结论"},
    ]})
    _prev_snapshot(conv, ws, [])
    mgr = _mgr(conv, {"requires_file": False})
    _run(mgr.verify_step_outputs("仅给出分析结论", ws))
    assert conv.deathmatch_plan["steps"][0]["status"] == "done", "无文件义务的分析步仍允许完成"


def test_tool_output_receipt_step_can_complete_without_new_file(tmp_path):
    ws = str(tmp_path)
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "只读回读核对",
         "expected_output": "对话内字节核对结论", "boundary": "只读核验，不得创建核验文件",
         "evidence_spec": {"kind": "tool_output", "must_run": True,
                           "receipt": {"cmds": ["python verify.py"], "outputs": []}}},
    ]})
    _prev_snapshot(conv, ws, [])
    mgr = _mgr(conv, {"requires_file": False})
    tool_evidence = [SimpleNamespace(name="terminal", arguments={"command": "python verify.py"}, result=json.dumps({"return_code": 0, "stdout": "ALPHA\nBETA\nGAMMA\n"}), error=False)]
    res = _run(mgr.verify_step_outputs("真实工具回读结果正确", ws, tool_results=tool_evidence))
    assert conv.deathmatch_plan["steps"][0]["status"] == "done", f"tool_output receipt 不得变成隐藏文件门，issues={res.get('issues')}"


def test_tool_output_receipt_accepts_wrapped_exact_command_segments(tmp_path):
    ws = str(tmp_path)
    expected = [
        "pwd",
        "printf 'ALPHA\\nBETA\\nGAMMA\\n' > dm.txt",
        "wc -c dm.txt",
        "wc -l dm.txt",
    ]
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "写入并回读",
         "expected_output": "对话内字节核对结论", "boundary": "只读核验，不得创建核验文件",
         "evidence_spec": {"kind": "tool_output", "must_run": True,
                            "receipt": {"cmds": expected, "outputs": []}}},
    ]})
    _prev_snapshot(conv, ws, [])
    mgr = _mgr(conv, {"requires_file": False})
    actual = (
        "cd /workspace && pwd && printf 'ALPHA\\nBETA\\nGAMMA\\n' > dm.txt "
        "&& echo '--- wc -c ---' && wc -c dm.txt && echo '--- wc -l ---' "
        "&& wc -l dm.txt && od -c dm.txt && realpath dm.txt"
    )
    evidence = [SimpleNamespace(name="terminal", arguments={"command": actual}, result=json.dumps({"return_code": 0, "stdout": "17\n3\n"}), error=False)]
    res = _run(mgr.verify_step_outputs("组合命令中的每段预期命令均真实执行", ws, tool_results=evidence))
    assert conv.deathmatch_plan["steps"][0]["status"] == "done", f"逐段精确回执不得被包装命令误拒，issues={res.get('issues')}"


def test_tool_output_receipt_requires_real_tool_evidence(tmp_path):
    ws = str(tmp_path)
    conv = _conv_with_created(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "只读回读核对",
         "expected_output": "对话内字节核对结论", "boundary": "只读核验，不得创建核验文件",
         "evidence_spec": {"kind": "tool_output", "must_run": True,
                           "receipt": {"cmds": ["python verify.py"], "outputs": []}}},
    ]})
    _prev_snapshot(conv, ws, [])
    mgr = _mgr(conv, {"requires_file": False})
    res = _run(mgr.verify_step_outputs("只自称已回读但没有工具证据", ws, tool_results=[]))
    assert conv.deathmatch_plan["steps"][0]["status"] != "done", f"无真实工具输出不得完成，issues={res.get('issues')}"
    assert any("工具" in issue and "证据" in issue for issue in res.get("issues") or [])
    unrelated = [SimpleNamespace(name="memory", result=" unrelated memory read ", error=False)]
    res2 = _run(mgr.verify_step_outputs("用无关 memory 读取冒充回执", ws, tool_results=unrelated))
    assert conv.deathmatch_plan["steps"][0]["status"] != "done", f"无关工具结果不得满足 receipt：{res2.get('issues')}"
    wrong_command = [SimpleNamespace(name="terminal", arguments={"command": "echo ok"}, result="ok", error=False)]
    res3 = _run(mgr.verify_step_outputs("用无关 terminal 命令冒充回执", ws, tool_results=wrong_command))
    assert conv.deathmatch_plan["steps"][0]["status"] != "done", f"命令不匹配不得满足 receipt：{res3.get('issues')}"
    echo_only = [SimpleNamespace(name="terminal", arguments={"command": "echo 'python verify.py'"}, result="python verify.py", error=False)]
    res4 = _run(mgr.verify_step_outputs("只 echo 命令文本并未执行", ws, tool_results=echo_only))
    assert conv.deathmatch_plan["steps"][0]["status"] != "done", f"echo 命令文本不等于实际执行：{res4.get('issues')}"
    quoted = [SimpleNamespace(name="terminal", arguments={"command": "echo '&&' python verify.py"}, result="&& python verify.py", error=False)]
    res5 = _run(mgr.verify_step_outputs("quoted separator 后没有执行目标命令", ws, tool_results=quoted))
    assert conv.deathmatch_plan["steps"][0]["status"] != "done", f"quoted && 不得被当成真实执行分隔符：{res5.get('issues')}"
    short_circuit = [SimpleNamespace(name="terminal", arguments={"command": "false && python verify.py; echo done"}, result="done", error=False)]
    res6 = _run(mgr.verify_step_outputs("前置 false 短路目标命令", ws, tool_results=short_circuit))
    assert conv.deathmatch_plan["steps"][0]["status"] != "done", f"前置短路不得满足 receipt：{res6.get('issues')}"
    nonzero = [SimpleNamespace(name="terminal", arguments={"command": "python verify.py"}, result='{"return_code": 1, "stdout": "partial"}', error=False)]
    res7 = _run(mgr.verify_step_outputs("目标命令返回非零", ws, tool_results=nonzero))
    assert conv.deathmatch_plan["steps"][0]["status"] != "done", f"非零 return_code 不得算成功回执：{res7.get('issues')}"
    failed_cd = [SimpleNamespace(name="terminal", arguments={"command": "cd /does-not-exist && python verify.py; echo done"}, result=json.dumps({"return_code": 0, "stdout": "done"}), error=False)]
    res8 = _run(mgr.verify_step_outputs("cd 失败后用分号 echo 掩盖", ws, tool_results=failed_cd))
    assert conv.deathmatch_plan["steps"][0]["status"] != "done", f"失败 cd + 分号 echo 不得满足 receipt：{res8.get('issues')}"
    masked = [SimpleNamespace(name="terminal", arguments={"command": "python verify.py; echo done"}, result=json.dumps({"return_code": 0, "stdout": "done"}), error=False)]
    res9 = _run(mgr.verify_step_outputs("验证失败被分号 echo 掩盖", ws, tool_results=masked))
    assert conv.deathmatch_plan["steps"][0]["status"] != "done", f"分号后的 echo 不得掩盖目标命令失败：{res9.get('issues')}"
    guidance = " ".join(res9.get("issues") or [])
    assert "&&" in guidance and "分号" in guidance, f"拒绝后必须给出可执行的 && 恢复指引：{guidance}"
    missing_rc = [SimpleNamespace(name="terminal", arguments={"command": "python verify.py"}, result='{"stdout": "ok"}', error=False)]
    res10 = _run(mgr.verify_step_outputs("回执缺 return_code", ws, tool_results=missing_rc))
    assert conv.deathmatch_plan["steps"][0]["status"] != "done", f"缺失 return_code 必须 fail closed：{res10.get('issues')}"
    bad_rc = [SimpleNamespace(name="terminal", arguments={"command": "python verify.py"}, result='{"return_code": "bad"}', error=False)]
    res11 = _run(mgr.verify_step_outputs("回执 return_code 非整数", ws, tool_results=bad_rc))
    assert conv.deathmatch_plan["steps"][0]["status"] != "done", f"非法 return_code 必须 fail closed：{res11.get('issues')}"


def test_tool_output_prompts_require_safe_and_chains():
    for name in ("PLAN_SYSTEM_PROMPT", "REPLANNER_SYSTEM_PROMPT", "LOCAL_PATCH_SYSTEM_PROMPT"):
        prompt = getattr(DeathmatchManager, name)
        assert "&&" in prompt and "分号" in prompt and "||" in prompt, (
            f"{name} 必须预防分号/短路回执并指导 && 顺序执行"
        )
