"""TDD tests: 死磕 DAG 波次 W0+W1+W2 — 契约（acceptance criteria）· 计划 v2 协议 ·
陈述冻结 · 计划核心审计 · goal 比较器/机械化阶梯 · 节点恢复 FSM · 失败方向记忆 ·
全局收敛 cap · PAUSED 包 · 事件日志 · 崩溃恢复。

设计依据：docs 方案（2026-09-18 综合版）；SGH 2604.11378 / Prove2Me 2608.28433 对照。
本文件按 A4.8 先行：实现前运行必须 RED。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_deathmatch_contract_v2.py -v
"""
import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.deathmatch_service as ds
from app.services.deathmatch_service import DeathmatchManager


# ── fixtures ──────────────────────────────────────────────────────────────

def _conv(**over):
    base = dict(
        id="conv-dag",
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
    )
    base.update(over)
    return SimpleNamespace(**base)


def _mgr(conv, judge=("continue", "not done", False, False, False)):
    mgr = DeathmatchManager(conv)
    mgr.get_continuation_prompt = lambda last_response="": "cont"
    mgr._record_reflection = lambda *a, **k: None
    mgr.collect_final_deliverables_from_messages = lambda *a, **k: asyncio.sleep(0, result=[])
    mgr._maybe_harness_repair = lambda *a, **k: asyncio.sleep(0, result=False)
    ds._call_judge_llm = lambda goal, resp, **k: asyncio.sleep(0, result=judge)
    return mgr


@pytest.fixture(autouse=True)
def _restore_judge():
    orig = ds._call_judge_llm
    ds._CREATIVE_GOAL_CACHE[ds._normalize_goal_key("测试目标：产出一份报告")] = False
    yield
    ds._call_judge_llm = orig
    ds._CREATIVE_GOAL_CACHE.pop(ds._normalize_goal_key("测试目标：产出一份报告"), None)


def _run(coro):
    return asyncio.run(coro)


# ── W0: 死配置清理 ─────────────────────────────────────────────────────────

def test_w0_plan_exploration_dead_config_removed():
    assert not hasattr(ds.config, "deathmatch_plan_exploration_enabled")
    assert not hasattr(ds.config, "deathmatch_plan_exploration_max_steps")
    import tomllib
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.toml")
    with open(cfg_path, "rb") as fh:
        toml = tomllib.load(fh)
    dm = toml.get("deathmatch", {})
    assert "plan_exploration_enabled" not in dm
    assert "plan_exploration_max_steps" not in dm


# ── W1a: acceptance criteria 一等公民 ─────────────────────────────────────

_CRIT_JSON = json.dumps({"criteria": [
    {"text": "报告文件存在且非空", "type": "mechanical",
     "check": {"kind": "file", "path": "report.md", "min_bytes": 100}},
    {"text": "语气与目标受众一致", "type": "judgmental", "check": {"kind": "none"}},
]}, ensure_ascii=False)


def test_w1a_parse_and_normalize_criteria():
    crit = ds._parse_criteria_response(_CRIT_JSON)
    assert len(crit) == 2
    assert [c["id"] for c in crit] == ["c1", "c2"]
    assert crit[0]["type"] == "mechanical" and crit[0]["source"] == "system"
    assert crit[0]["check"]["kind"] == "file" and crit[0]["check"]["path"] == "report.md"
    assert crit[1]["type"] == "judgmental" and crit[1]["check"]["kind"] == "none"
    # 空文本仍丢弃；unknown kind 必须在 normalization 前明确拒绝（D-dm9）。
    crit2 = ds._parse_criteria_response(json.dumps({"criteria": [
        {"text": "", "type": "mechanical"},
        {"text": "x", "type": "judgmental", "check": {"kind": "none"}},
    ]}))
    assert len(crit2) == 1 and crit2[0]["check"]["kind"] == "none"
    with pytest.raises(ds.CriteriaCandidateRejected):
        ds._parse_criteria_response(json.dumps({"criteria": [
            {"text": "x", "type": "mechanical", "check": {"kind": "rm -rf", "path": "p"}},
        ]}))


def test_w1a_contract_hash_stable_and_sensitive():
    crit = ds._parse_criteria_response(_CRIT_JSON)
    h1 = ds._contract_hash("目标A", crit)
    h2 = ds._contract_hash("目标A", crit)
    h3 = ds._contract_hash("目标B", crit)
    assert h1 == h2 and h1 != h3 and len(h1) == 64


def test_w1a_merge_user_subgoals_dedup():
    sys_c = ds._parse_criteria_response(_CRIT_JSON)
    merged = ds._merge_criteria(sys_c, ["报告文件存在且非空", "字数不少于 3000"])
    assert len(merged) == 3, merged
    users = [c for c in merged if c["source"] == "user"]
    assert len(users) == 1 and users[0]["text"] == "字数不少于 3000"
    assert users[0]["type"] == "judgmental"
    ids = [c["id"] for c in merged]
    assert len(ids) == len(set(ids))


def test_w1a_synthesize_persists_and_injects():
    conv = _conv()
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return _CRIT_JSON

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    crit = _run(mgr._synthesize_acceptance_criteria())
    assert crit and conv.deathmatch_acceptance_criteria == crit
    goal_block = mgr._goal_with_criteria()
    assert "报告文件存在且非空" in goal_block
    assert "report.md" in goal_block
    handoff = mgr.generate_handoff_document()
    assert "报告文件存在且非空" in handoff, "criteria 必须进入交接文档（压缩免疫）"
    # 用户 subgoals 合并进注入
    conv.deathmatch_subgoals = ["字数不少于 3000"]
    assert "字数不少于 3000" in mgr._goal_with_criteria()


def test_w1a_synthesize_fail_open():
    conv = _conv()
    mgr = DeathmatchManager(conv)

    async def boom(llm, system_prompt, user_prompt, **kwargs):
        raise RuntimeError("llm down")

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = boom
    crit = _run(mgr._synthesize_acceptance_criteria())
    assert crit == [] and (conv.deathmatch_acceptance_criteria or []) == []


# ── W1b: 计划 v2 协议 ─────────────────────────────────────────────────────

def test_w1b_plan_v2_normalization_defaults_and_inference():
    raw = json.dumps({"steps": [{
        "id": "s1", "description": "收集资料", "expected_output": "资料摘要",
        "verification_method": "gate: test -f notes.md", "delegable": True,
        "tools": ["web_search"],
    }]}, ensure_ascii=False)
    plan = DeathmatchManager(_conv())._parse_plan(raw)
    s = plan["steps"][0]
    assert s["kind"] == "research", "delegable 步骤应推断为 research"
    assert s["parallel_safe"] is False
    assert s["writes"] == []
    assert s["done_check"]["mode"] == "gate" and "notes.md" in s["done_check"]["cmd"]
    assert s["attempts"] == 0 and s["recovery"] == "idle"

    raw2 = json.dumps({"steps": [{
        "id": "w1", "description": "写报告", "expected_output": "report.md",
        "writes": ["report.md"], "kind": "write",
        "done_check": {"mode": "file", "path": "report.md", "min_bytes": 100},
    }]}, ensure_ascii=False)
    s2 = DeathmatchManager(_conv())._parse_plan(raw2)["steps"][0]
    assert s2["kind"] == "write" and s2["writes"] == ["report.md"]
    assert s2["done_check"] == {"mode": "file", "path": "report.md", "min_bytes": 100}


def test_w1b_plan_v1_backward_compat():
    old = json.dumps({"steps": [
        {"id": "s1", "description": "旧步骤", "expected_output": "x",
         "verification_method": "word_count", "dependencies": [], "status": "pending"},
    ]}, ensure_ascii=False)
    plan = DeathmatchManager(_conv())._parse_plan(old)
    assert plan and plan["steps"][0]["kind"] in ("write", "research", "verify", "synthesize")
    issues = ds._validate_plan_protocol(plan, valid_tool_names=None)
    assert issues == [], issues


def test_w1b_graph_validation_rejects_bad_deps():
    def _plan(steps):
        return {"steps": steps}

    base = {"description": "d", "expected_output": "e", "kind": "write"}
    dangling = _plan([dict(base, id="s1", dependencies=["s9"])])
    issues = ds._validate_plan_protocol(dangling, None)
    assert any("依赖" in i for i in issues), issues

    selfdep = _plan([dict(base, id="s1", dependencies=["s1"])])
    assert any("依赖" in i for i in ds._validate_plan_protocol(selfdep, None))

    cycle = _plan([dict(base, id="s1", dependencies=["s2"]),
                   dict(base, id="s2", dependencies=["s1"])])
    assert any("环" in i for i in ds._validate_plan_protocol(cycle, None))

    bad_kind = _plan([dict(base, id="s1", kind="magic")])
    assert any("kind" in i for i in ds._validate_plan_protocol(bad_kind, None))

    bad_check = _plan([dict(base, id="s1", done_check={"mode": "gate"})])
    assert any("done_check" in i for i in ds._validate_plan_protocol(bad_check, None))

    bad_check2 = _plan([dict(base, id="s1", done_check={"mode": "file"})])
    assert any("done_check" in i for i in ds._validate_plan_protocol(bad_check2, None))


# ── W1c: 陈述冻结 ─────────────────────────────────────────────────────────

def test_w1c_statement_hash_stable_and_field_sensitive():
    step = {"id": "s1", "description": "a", "expected_output": "b",
            "boundary": "", "dependencies": ["s0"], "writes": ["x.md"]}
    h = ds.step_statement_hash(step)
    assert h == ds.step_statement_hash(dict(step)) and len(h) == 64
    mutated = dict(step, description="CHANGED")
    assert ds.step_statement_hash(mutated) != h
    reordered = dict(step, dependencies=["s0"], writes=["x.md"])
    assert ds.step_statement_hash(reordered) == h, "同内容不同插入序不影响 hash"


def test_w1c_done_steps_preserved_validator():
    old = [{"id": "s1", "description": "a", "expected_output": "b",
            "boundary": "", "dependencies": [], "writes": [], "status": "done"}]
    ok = [dict(old[0]), {"id": "s2", "description": "c", "expected_output": "d",
                         "status": "pending"}]
    assert ds._validate_done_steps_preserved(old, ok) == []
    mutated = [dict(old[0], description="悄悄改掉"), {"id": "s2", "description": "c"}]
    issues = ds._validate_done_steps_preserved(old, mutated)
    assert issues and any("陈述" in i for i in issues), issues
    dropped = [{"id": "s2", "description": "c"}]
    assert ds._validate_done_steps_preserved(old, dropped)


def test_w1c_settle_records_statement_hash():
    conv = _conv()
    mgr = DeathmatchManager(conv)
    mgr._record_settled({"type": "step_complete",
                         "summary": "s1 完成",
                         "step": {"id": "s1", "description": "a",
                                  "expected_output": "b"}})
    ledger = conv.deathmatch_settled_ledger or []
    assert ledger and ledger[-1].get("statement_hash"), ledger


def test_w1c_replan_rejects_statement_mutation():
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "done", "description": "a", "expected_output": "b",
         "boundary": "", "dependencies": [], "writes": []},
        {"id": "s2", "status": "pending", "description": "c", "expected_output": "d",
         "boundary": "", "dependencies": ["s1"], "writes": []},
    ]})
    mgr = DeathmatchManager(conv)

    mutated = json.dumps({"steps": [
        {"id": "s1", "status": "done", "description": "a 被改写",
         "expected_output": "b", "kind": "write"},
        {"id": "s2", "status": "pending", "description": "c", "expected_output": "d"},
    ]}, ensure_ascii=False)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return mutated

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    out = _run(mgr.replan({"status": "partial", "issues": ["缺文件"], "retry_instruction": "补"}))
    assert out is None, "陈述被改写的 replan 必须被拒绝"
    events = conv.deathmatch_events or []
    assert any(e.get("type") == "statement_violation" for e in events), events


# ── W1d: 计划核心审计 ─────────────────────────────────────────────────────

_PLAN_JSON = json.dumps({"steps": [
    {"id": "s1", "description": "写报告", "expected_output": "report.md",
     "kind": "write", "writes": ["report.md"], "dependencies": []},
]}, ensure_ascii=False)


def test_w1d_audit_issues_trigger_one_repair():
    conv = _conv()
    mgr = DeathmatchManager(conv)
    calls = {"gen": 0, "audit": 0}

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        calls["gen"] += 1
        return _PLAN_JSON

    async def fake_audit(plan):
        calls["audit"] += 1
        return ["缺少对'字数≥3000'的覆盖"] if calls["audit"] == 1 else []

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    mgr._audit_plan_core = fake_audit
    plan = _run(mgr.generate_goal_plan(db=None))
    assert plan is not None
    assert calls["audit"] == 2 and calls["gen"] == 2, calls
    assert conv.deathmatch_plan is not None
    events = conv.deathmatch_events or []
    assert any(e.get("type") == "plan_audit" for e in events), events


def test_w1d_unresolved_contradictions_reject_plan():
    conv = _conv()
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return _PLAN_JSON

    async def fake_audit(plan):
        return ["步骤写 18 字节，但原始目标明确为 17 字节"]

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    mgr._audit_plan_core = fake_audit
    plan = _run(mgr.generate_goal_plan(db=None))
    assert plan is None, "审计仍证明硬冲突时不得执行该计划"
    assert conv.deathmatch_plan is None
    assert any(e.get("type") == "plan_rejected" for e in conv.deathmatch_events or [])


def test_w1d_repair_parse_failure_rejects_original_plan():
    conv = _conv()
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return _PLAN_JSON

    async def fake_audit(plan):
        return ["硬冲突"]

    calls = {"parse": 0}

    async def no_repair(*args, **kwargs):
        calls["parse"] += 1
        return json.loads(_PLAN_JSON) if calls["parse"] == 1 else None

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    mgr._audit_plan_core = fake_audit
    mgr._parse_plan_with_repair = no_repair
    plan = _run(mgr.generate_goal_plan(db=None))
    assert plan is None, "修复解析失败不得保留初审已拒绝计划"
    assert conv.deathmatch_plan is None
    assert any(e.get("type") == "plan_rejected" for e in conv.deathmatch_events or [])


def test_w1d_second_audit_failure_rejects_plan():
    conv = _conv()
    mgr = DeathmatchManager(conv)
    calls = {"audit": 0}

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return _PLAN_JSON

    async def fake_audit(plan):
        calls["audit"] += 1
        if calls["audit"] == 1:
            return ["硬冲突"]
        raise RuntimeError("second audit unavailable")

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    mgr._audit_plan_core = fake_audit
    plan = _run(mgr.generate_goal_plan(db=None))
    assert plan is None, "二次审计异常不得保留初审已拒绝计划"
    assert conv.deathmatch_plan is None
    assert any(e.get("type") == "plan_rejected" for e in conv.deathmatch_events or [])


def test_w1d_audit_fail_open(monkeypatch):
    conv = _conv()
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return _PLAN_JSON

    async def boom(plan):
        raise RuntimeError("audit llm down")

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    mgr._audit_plan_core = boom
    plan = _run(mgr.generate_goal_plan(db=None))
    assert plan is not None, "审计失败必须 fail-open"


# ── W1e: goal 比较器 + 机械化阶梯 ─────────────────────────────────────────

def test_w1e_mechanical_checks_filter():
    crit = ds._parse_criteria_response(_CRIT_JSON)
    mech = ds._mechanical_checks(crit)
    assert len(mech) == 1 and mech[0]["check"]["kind"] == "file"


def test_w1e_goal_comparator_file_checks(tmp_path):
    conv = _conv(deathmatch_acceptance_criteria=ds._parse_criteria_response(_CRIT_JSON))
    mgr = DeathmatchManager(conv)
    issues = _run(mgr._run_goal_comparator(str(tmp_path)))
    assert issues and "report.md" in " ".join(issues), issues
    (tmp_path / "report.md").write_text("x" * 200, encoding="utf-8")
    assert _run(mgr._run_goal_comparator(str(tmp_path))) == []


def test_w1e_finalize_blocked_by_failed_comparator():
    conv = _conv(
        deathmatch_acceptance_criteria=ds._parse_criteria_response(_CRIT_JSON),
        deathmatch_plan={"steps": [
            {"id": "s1", "status": "done", "description": "写报告",
             "expected_output": "report.md", "kind": "write"},
        ]},
    )
    mgr = _mgr(conv, judge=("done", "报告已交付 report.md", False, False, False))
    mgr.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "complete", "progress": True, "current_step": "s1",
        "completed_steps": ["s1"], "issues": []})
    d = _run(mgr.evaluate_after_turn("完成", workspace_path="/tmp/definitely-missing-dir"))
    assert d["should_continue"] is True and d["status"] == "active", d
    assert "report.md" in (d.get("reason") or "") + (d.get("continuation_prompt") or ""), d
    events = conv.deathmatch_events or []
    assert any(e.get("type") == "comparator" for e in events), events


def test_w1e_finalize_step_done_check_blocks_and_finalize_event():
    # 1) 未完成步骤的机械 done_check 失败 → 不 finalize
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "done", "description": "a", "expected_output": "a.txt"},
        {"id": "s2", "status": "pending", "description": "b", "expected_output": "b.txt",
         "kind": "write", "done_check": {"mode": "file", "path": "b.txt", "min_bytes": 1}},
    ]})
    mgr = _mgr(conv, judge=("done", "全部完成", False, False, False))
    mgr.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "partial", "progress": True, "current_step": "s2",
        "completed_steps": ["s1"], "issues": []})
    d = _run(mgr.evaluate_after_turn("完成", workspace_path="/tmp/definitely-missing-dir"))
    assert d["should_continue"] is True, d

    # 2) 无机械检查的未完成步骤：维持原 finalize 行为并记录事件
    conv2 = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "done", "description": "a", "expected_output": "a.txt"},
        {"id": "s2", "status": "pending", "description": "b", "expected_output": "b.txt"},
    ]})
    conv2.deathmatch_acceptance_criteria = []
    mgr2 = _mgr(conv2, judge=("done", "全部完成", False, False, False))
    mgr2._reconcile_completion = lambda *a, **k: asyncio.sleep(0, result=("finalize", "证据充分"))
    mgr2.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "partial", "progress": True, "current_step": "s2",
        "completed_steps": ["s1"], "issues": []})
    d2 = _run(mgr2.evaluate_after_turn("完成", workspace_path="/tmp/definitely-missing-dir"))
    assert d2["status"] == "done", d2
    events = conv2.deathmatch_events or []
    assert any(e.get("type") == "finalized_without_step_evidence" for e in events), events
    assert conv2.deathmatch_last_verification_result["status"] == "complete"
    assert conv2.deathmatch_last_verification_result["completed_steps"] == ["s1", "s2"]
    assert conv2.deathmatch_last_verification_result["issues"] == []


# ── W2a: 节点恢复 FSM ─────────────────────────────────────────────────────

def test_w2a_recovery_action_ladder():
    assert ds._recovery_action(1, 2) == "local_retry"
    assert ds._recovery_action(2, 2) == "local_retry"
    assert ds._recovery_action(3, 2) == "local_patch"
    assert ds._recovery_action(4, 2) == "replan"
    assert ds._recovery_action(1, 0) == "local_patch"


def test_w2a_local_patch_rejects_protocol_violation():
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "a", "expected_output": "b",
         "dependencies": [], "writes": []},
    ]})
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return json.dumps({"steps": [
            {"id": "s1", "status": "pending", "description": "a2", "expected_output": "b2",
             "dependencies": ["s9"]},
        ]}, ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    ok = _run(mgr._local_patch_step(conv.deathmatch_plan["steps"][0], ["缺文件"]))
    assert ok is False, "非法 patch（悬空依赖）必须被拒绝"
    assert conv.deathmatch_plan["steps"][0]["description"] == "a", "拒绝时原计划不变"


def test_w2a_local_patch_applies_valid_rewrite(monkeypatch):
    # fix-round 1（评审 A-2 同病，ADR D-17）：夹 judge_json=不可达降级——
    # live judge 可达时数值触发词拒 patch=非密封 flaky，断言不动。
    import app.services.agentic_judge as aj

    async def _none_judge(system_prompt, user_prompt, **kwargs):
        return None

    monkeypatch.setattr(aj, "judge_json", _none_judge)
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "a", "expected_output": "b",
         "dependencies": [], "writes": []},
    ]})
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return json.dumps({"steps": [
            {"id": "s1", "status": "pending", "description": "更细的步骤",
             "expected_output": "b", "dependencies": []},
        ]}, ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    ok = _run(mgr._local_patch_step(conv.deathmatch_plan["steps"][0], ["缺文件"]))
    assert ok is True
    assert conv.deathmatch_plan["steps"][0]["description"] == "更细的步骤"
    events = conv.deathmatch_events or []
    assert any(e.get("type") == "local_patch" for e in events), events


def test_w2a_recovery_integration_local_retry_then_patch():
    """端到端：同一节点连续无进展 → 两次 local_retry 后第三次触发
    local_patch 并返回 continue 决策（跳过当轮 legacy stall）。"""
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "a", "expected_output": "b",
         "dependencies": [], "writes": []},
    ]})
    mgr = _mgr(conv, judge=("continue", "未完成", False, False, False))
    mgr.replan = lambda vr: asyncio.sleep(0, result=None)
    mgr.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "partial", "progress": False, "current_step": "s1",
        "issues": ["缺文件"], "completed_steps": [], "diagnosis": "b.txt 不存在"})
    patch_calls = []

    async def fake_patch(step, issues):
        patch_calls.append(list(issues))
        return True

    mgr._local_patch_step = fake_patch
    d1 = _run(mgr.evaluate_after_turn("working", workspace_path="/tmp/x"))
    assert d1["should_continue"] is True
    d2 = _run(mgr.evaluate_after_turn("working", workspace_path="/tmp/x"))
    assert d2["should_continue"] is True
    d3 = _run(mgr.evaluate_after_turn("working", workspace_path="/tmp/x"))
    assert patch_calls, "第三个无进展轮必须触发 local_patch"
    assert "local_patch" in (d3.get("reason") or ""), d3
    assert conv.deathmatch_plan["steps"][0]["attempts"] == 3
    events = conv.deathmatch_events or []
    actions = [e.get("action") for e in events if e.get("type") == "node_recovery"]
    assert actions[:3] == ["local_retry", "local_retry", "local_patch"], actions


def test_w2b_no_progress_cap_integration_gates_resumable():
    """端到端：连续无进展 replan 达 cap → 可恢复 human_gate + PAUSED 包。"""
    conv = _conv()
    mgr = _mgr(conv, judge=("continue", "未完成", False, False, False))
    mgr.replan = lambda vr: asyncio.sleep(0, result=None)
    mgr.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "partial", "progress": False, "current_step": "s1",
        "issues": ["缺文件"], "completed_steps": []})

    async def no_patch(step, issues):
        return False

    mgr._local_patch_step = no_patch
    cap = ds.config.deathmatch_no_progress_replan_cap
    assert cap and cap > 0
    d = None
    for _ in range(cap + 2):
        d = _run(mgr.evaluate_after_turn("working", workspace_path="/tmp/x"))
        if d["status"] == "human_gate":
            break
    assert d["status"] == "human_gate" and d["should_continue"] is False, d
    assert conv.deathmatch_status == "human_gate"
    assert conv.deathmatch_pause_state and conv.deathmatch_pause_state.get("gate")
    assert "无进展" in (d.get("message") or "") or "断路" in (d.get("message") or "")


# ── W2b: failed_directions + 全局收敛 cap ─────────────────────────────────

def test_w2b_failed_direction_forbidden_after_threshold():
    conv = _conv()
    mgr = DeathmatchManager(conv)
    mgr._record_failed_direction("验证器反复报同一缺文件问题", family="missing-file")
    mgr._record_failed_direction("验证器反复报同一缺文件问题", family="missing-file")
    entry = mgr._record_failed_direction("验证器反复报同一缺文件问题", family="missing-file")
    assert entry["count"] == 3 and entry["forbidden"] is True
    block = mgr._failed_directions_block()
    assert "验证器反复报同一缺文件问题" in block
    assert "禁止" in block or "forbidden" in block.lower()


def test_w2b_no_progress_replan_cap():
    conv = _conv(deathmatch_no_progress_replans=0)
    mgr = DeathmatchManager(conv)
    assert mgr._bump_no_progress_replan() is False
    assert mgr._bump_no_progress_replan() is False
    conv.deathmatch_no_progress_replans = 2
    assert mgr._bump_no_progress_replan() is True, "达到上限必须触发收敛断路器"
    mgr._reset_no_progress_replan()
    assert conv.deathmatch_no_progress_replans == 0


# ── W2c: PAUSED 包 + 事件日志 ─────────────────────────────────────────────

def test_w2c_pause_packet_written_and_cleared():
    conv = _conv()
    mgr = DeathmatchManager(conv)
    mgr.trigger_human_gate("目标受阻：缺少凭据", report={"suggested_actions": ["补充信息"]})
    packet = conv.deathmatch_pause_state
    assert packet and packet["gate"] and packet["question"]
    assert packet["options"] and packet["default_if_continue"]
    assert packet["state"]["turn"] == 0
    conv.deathmatch_status = "human_gate"
    mgr.resume()
    assert conv.deathmatch_pause_state is None, "resume 必须清除 PAUSED 包"


def test_w2c_events_append_and_capped():
    conv = _conv()
    mgr = DeathmatchManager(conv)
    cap = ds.config.deathmatch_events_cap
    for i in range(cap + 10):
        mgr._record_event("test_evt", idx=i)
    events = conv.deathmatch_events or []
    assert len(events) == cap
    assert events[-1]["type"] == "test_evt" and events[-1]["idx"] == cap + 9


def test_w2c_events_recorded_for_core_decisions():
    conv = _conv(
        deathmatch_acceptance_criteria=ds._parse_criteria_response(_CRIT_JSON),
        deathmatch_plan={"steps": [
            {"id": "s1", "status": "done", "description": "写报告",
             "expected_output": "report.md", "kind": "write"},
        ]},
    )
    mgr = _mgr(conv, judge=("done", "报告已交付", False, False, False))
    mgr.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "complete", "progress": True, "current_step": "s1",
        "completed_steps": ["s1"], "issues": []})
    _run(mgr.evaluate_after_turn("完成", workspace_path="/tmp/definitely-missing-dir"))
    types = {e.get("type") for e in (conv.deathmatch_events or [])}
    assert "comparator" in types, types


# ── W2d: 启动崩溃恢复 ──────────────────────────────────────────────────────

def test_w2d_stale_active_normalized_to_paused():
    conv = _conv(deathmatch_status="active")
    changed = ds._normalize_stale_active(conv)
    assert changed is True
    assert conv.deathmatch_status == "paused"
    assert conv.deathmatch_pause_state and conv.deathmatch_pause_state["gate"] == "crash-recovery"
    assert "中断" in (conv.deathmatch_reason or "")

    done = _conv(deathmatch_status="done")
    assert ds._normalize_stale_active(done) is False
    assert done.deathmatch_status == "done"


def test_w2d_recover_driver(monkeypatch):
    convs = [_conv(deathmatch_status="active"), _conv(deathmatch_status="paused")]

    async def fake_load(db):
        return convs

    monkeypatch.setattr(ds, "_load_stale_active", fake_load)
    n = _run(ds.recover_stale_active_deathmatch(db=None))
    assert n == 1 and convs[0].deathmatch_status == "paused"
    assert convs[1].deathmatch_status == "paused"


# ── A4.9 修复轮（review round 1 findings 回归）───────────────────────────

def test_fix_local_patch_preserves_attempts(monkeypatch):
    # fix-round 1（评审 A-2 同病，ADR D-17）：夹 judge_json=不可达降级——
    # live judge 可达时数值触发词拒 patch=非密封 flaky，断言不动。
    import app.services.agentic_judge as aj

    async def _none_judge(system_prompt, user_prompt, **kwargs):
        return None

    monkeypatch.setattr(aj, "judge_json", _none_judge)
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "a", "expected_output": "b",
         "dependencies": [], "writes": [], "attempts": 3, "recovery": "local_patch"},
    ]})
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return json.dumps({"steps": [
            {"id": "s1", "status": "pending", "description": "重写后",
             "expected_output": "b", "dependencies": []},
        ]}, ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    ok = _run(mgr._local_patch_step(conv.deathmatch_plan["steps"][0], ["缺文件"]))
    assert ok is True
    assert conv.deathmatch_plan["steps"][0]["attempts"] == 3, (
        "attempts 必须保留（否则 local_patch 会永久重臂）"
    )
    assert conv.deathmatch_plan["steps"][0]["recovery"] == "local_patch_applied"


def test_fix_merge_criteria_unique_ids():
    sys_c = [{"id": "u1", "text": "系统条目", "type": "judgmental",
              "source": "system", "check": {"kind": "none"}}]
    merged = ds._merge_criteria(sys_c, ["用户条目"])
    ids = [c["id"] for c in merged]
    assert len(ids) == len(set(ids)), ids


def test_fix_comparator_fail_closed_on_missing_workspace():
    conv = _conv(deathmatch_acceptance_criteria=ds._parse_criteria_response(_CRIT_JSON))
    mgr = DeathmatchManager(conv)
    issues = _run(mgr._run_goal_comparator(""))
    assert issues and "工作区" in issues[0], issues


def test_fix_failed_directions_injected_in_continuation():
    conv = _conv()
    mgr = DeathmatchManager(conv)
    mgr._record_failed_direction("反复缺同一文件", family="missing-file")
    prompt = mgr.get_continuation_prompt("")
    assert "failed_directions" in prompt and "反复缺同一文件" in prompt, prompt[:400]


def test_fix_gate_command_wrapper_imports_sys(monkeypatch):
    import app.services.code_execution_service as ces

    captured = {}

    class _FakeResult:
        return_code = 0
        stdout = "[gate] exit=0\nok"
        stderr = ""
        error = None

    class _FakeSvc:
        async def execute_python(self, code, **kwargs):
            captured["code"] = code
            return _FakeResult()

    monkeypatch.setattr(ces, "CodeExecutionService", _FakeSvc)
    conv = _conv()
    mgr = DeathmatchManager(conv)
    issues = _run(mgr._run_gate_command("true", "/tmp"))
    assert issues == [], issues
    assert "import subprocess, os, sys" in captured["code"], "wrapper 必须导入 sys（否则任何 gate 必失败）"


def test_fix_resume_resets_breaker():
    conv = _conv(deathmatch_status="human_gate", deathmatch_no_progress_replans=3)
    mgr = DeathmatchManager(conv)
    mgr._accumulate_wall_time = lambda: None
    mgr._freeze_wall_time = lambda: None
    mgr.resume()
    assert conv.deathmatch_no_progress_replans == 0


def test_fix_comparator_blocks_feed_breaker_and_gate():
    conv = _conv(
        deathmatch_acceptance_criteria=ds._parse_criteria_response(_CRIT_JSON),
        deathmatch_plan={"steps": [
            {"id": "s1", "status": "done", "description": "写报告",
             "expected_output": "report.md", "kind": "write"},
        ]},
    )
    mgr = _mgr(conv, judge=("done", "报告已交付 report.md", False, False, False))
    mgr.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "complete", "progress": True, "current_step": "s1",
        "completed_steps": ["s1"], "issues": []})
    cap = ds.config.deathmatch_no_progress_replan_cap
    d = None
    for _ in range(cap + 1):
        d = _run(mgr.evaluate_after_turn("完成", workspace_path="/tmp/definitely-missing-dir"))
        if d["status"] == "human_gate":
            break
    assert d["status"] == "human_gate", d
    assert conv.deathmatch_status == "human_gate"
    assert conv.deathmatch_pause_state and conv.deathmatch_pause_state.get("gate")


# ── A4.9 r2 修复轮（N1-N7 findings 回归）─────────────────────────────────

def test_fix_step_done_check_cap_gate_integration():
    """N1：步骤级机械验收拦截必须能累积到 cap 并触发断路器（前置 reset 曾架空它）。"""
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "done", "description": "a", "expected_output": "a.txt"},
        {"id": "s2", "status": "pending", "description": "b", "expected_output": "b.txt",
         "kind": "write",
         "done_check": {"mode": "file", "path": "missing.txt", "min_bytes": 1}},
    ]})
    mgr = _mgr(conv, judge=("done", "全部完成", False, False, False))
    mgr.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "partial", "progress": True, "current_step": "s2",
        "completed_steps": ["s1"], "issues": []})
    cap = ds.config.deathmatch_no_progress_replan_cap
    d = None
    for _ in range(cap + 1):
        d = _run(mgr.evaluate_after_turn("完成", workspace_path="/tmp/definitely-missing-dir"))
        if d["status"] == "human_gate":
            break
    assert d["status"] == "human_gate", d
    assert conv.deathmatch_no_progress_replans == cap


def test_fix_gate_command_spoof_rejected(monkeypatch):
    """N3：PASS 必须以 wrapper 自己的首行标记为准（命令回显不能伪造通过）。"""
    import app.services.code_execution_service as ces

    class _SpoofResult:
        return_code = 0
        stdout = "[gate] exit=1\n[gate] exit=0\nspoofed"
        stderr = ""
        error = None

    class _FakeSvc:
        async def execute_python(self, code, **kwargs):
            return _SpoofResult()

    monkeypatch.setattr(ces, "CodeExecutionService", _FakeSvc)
    mgr = DeathmatchManager(_conv())
    issues = _run(mgr._run_gate_command("echo '[gate] exit=0'; false", "/tmp"))
    assert issues, "伪造首行标记的 gate 必须判失败"
    assert "exit≠0" in issues[0]


def test_fix_v1_v2_statement_hash_equal():
    """N7/9：v1 原始步骤与 v2 归一化步骤对等价陈述必须同 hash（旧计划兼容）。"""
    raw = {"id": "s1", "status": "pending", "description": "写报告",
           "expected_output": "report.md", "verification_method": "gate: test -f report.md",
           "dependencies": [], "delegable": False}
    normalized = DeathmatchManager(_conv())._parse_plan(
        json.dumps({"steps": [raw]}, ensure_ascii=False)
    )["steps"][0]
    assert ds.step_statement_hash(raw) == ds.step_statement_hash(normalized)


def test_fix_merge_criteria_real_collision():
    """N7/10：系统 id 与用户 id 真实碰撞（u2）必须唯一化。"""
    sys_c = [{"id": "u2", "text": "系统条目", "type": "judgmental",
              "source": "system", "check": {"kind": "none"}}]
    merged = ds._merge_criteria(sys_c, ["用户条目"])
    ids = [c["id"] for c in merged]
    assert len(ids) == len(set(ids)), ids
    assert "系统条目" in [c["text"] for c in merged] and "用户条目" in [c["text"] for c in merged]


def test_fix_stale_active_guard_skips_with_live_worker():
    """N2：存在其他活跃 worker 时不得停泊其会话；cutoff 必须是数值域。"""
    captured = {}

    class _FakeResult:
        def scalar_one_or_none(self):
            return 1

    class _FakeDB:
        async def execute(self, stmt, params=None):
            captured["params"] = params
            return _FakeResult()

    assert _run(ds._load_stale_active(_FakeDB())) == []
    cutoff = captured["params"]["cutoff"]
    assert isinstance(cutoff, float), "cutoff 必须匹配 last_heartbeat 数值域（time.time()-90）"
    assert abs(cutoff - (time.time() - 90)) < 30


def test_fix_resume_from_partial_clears_w2_state():
    """N4：partial_complete 恢复同样清理 PAUSED 包并重置断路器。"""
    conv = _conv(
        deathmatch_status="partial_complete",
        deathmatch_no_progress_replans=2,
        deathmatch_pause_state={"gate": "stale", "question": "q", "options": [],
                                "default_if_continue": "d", "state": {}},
    )
    mgr = DeathmatchManager(conv)
    mgr._accumulate_wall_time = lambda: None
    mgr._freeze_wall_time = lambda: None
    mgr.resume_from_partial()
    assert conv.deathmatch_no_progress_replans == 0
    assert conv.deathmatch_pause_state is None


def test_fix_crash_park_freezes_wall_clock():
    """N5：崩溃停泊必须折算已用墙钟并停表（停机时间不得计费）。"""
    from datetime import datetime, timedelta
    conv = _conv(
        deathmatch_status="active",
        deathmatch_wall_time_started_at=datetime.utcnow() - timedelta(seconds=30),
        deathmatch_wall_time_used_seconds=100,
    )
    assert ds._normalize_stale_active(conv) is True
    assert conv.deathmatch_wall_time_started_at is None
    assert conv.deathmatch_wall_time_used_seconds >= 129


# ── 遗留项收口（deferred closure）───────────────────────────────────────

def test_fix_sync_fields_cover_manager_state():
    """A4.9 r1 Critical 收口：同步列表必须覆盖管理器可变的所有死磕字段
    （settled/reflections/墙钟/上下文/W1-W2 状态），且源缺失字段跳过不置空。"""
    required = {
        "deathmatch_settled_ledger", "deathmatch_reflections",
        "deathmatch_wall_time_started_at", "deathmatch_wall_time_used_seconds",
        "deathmatch_max_wall_time_seconds", "deathmatch_max_turns",
        "deathmatch_context_summary", "deathmatch_compressed_context",
        "deathmatch_acceptance_criteria", "deathmatch_failed_directions",
        "deathmatch_pause_state", "deathmatch_events",
        "deathmatch_no_progress_replans", "deathmatch_plan",
        "deathmatch_plan_version", "deathmatch_human_gate",
    }
    missing = required - set(ds.DEATHMATCH_SYNC_FIELDS)
    assert not missing, f"同步列表缺失管理器可变字段: {missing}"
    # A4.9 closure Minor: every synced name must be a real Conversation column
    # (a typo would otherwise silently no-op and the test would still pass).
    from app.db.database import Conversation
    cols = {c.name for c in Conversation.__table__.columns}
    bad = [f for f in ds.DEATHMATCH_SYNC_FIELDS if f not in cols]
    assert not bad, f"同步列表字段不是 Conversation 列: {bad}"
    assert len(ds.DEATHMATCH_SYNC_FIELDS) == 28
    src = SimpleNamespace(**{f: f"v:{f}" for f in ds.DEATHMATCH_SYNC_FIELDS})
    tgt = SimpleNamespace()
    ds.sync_deathmatch_state(tgt, src)
    for f in ds.DEATHMATCH_SYNC_FIELDS:
        assert getattr(tgt, f) == f"v:{f}"
    tgt2 = SimpleNamespace(deathmatch_status="keep")
    ds.sync_deathmatch_state(tgt2, SimpleNamespace())
    assert tgt2.deathmatch_status == "keep", "源缺失字段必须跳过而非置空"


def test_fix_infra_turn_does_not_reset_breaker():
    """A4.9 r2 N6：judge infra 轮的 verifier 进展不得清全局收敛断路器（P1-7）。"""
    conv = _conv(deathmatch_no_progress_replans=2)
    mgr = _mgr(conv, judge=("continue", "judge infra error", False, True, False))
    mgr.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "partial", "progress": True, "current_step": "s1",
        "completed_steps": [], "issues": []})
    d = _run(mgr.evaluate_after_turn("working", workspace_path="/tmp/x"))
    assert d["should_continue"] is True
    assert conv.deathmatch_no_progress_replans == 2, "infra 轮不得清断路器"


def test_fix_done_check_gate_off_is_skipped(monkeypatch):
    """存量语义钉死：command gate 未启用时，gate 类 done_check 跳过不执行。
    A4.9 closure Minor: 显式 monkeypatch 门禁开关，不依赖 config.toml 当前值。"""
    monkeypatch.setattr(
        type(ds.config), "deathmatch_verify_command_gate_enabled",
        property(lambda self: False),
    )
    conv = _conv()
    mgr = DeathmatchManager(conv)
    step = {"id": "s1", "done_check": {"mode": "gate", "cmd": "false"}}
    assert _run(mgr._run_step_done_checks(step, "/tmp")) == []


# ── 残余项收口：同步竞态（乐观逐字段合并）─────────────────────────────────

def test_residual_merge_sync_control_field_external_wins_and_freezes():
    """外部动作（pause/stop/resume）改写的控制字段必须获胜并冻结；
    数据字段外部获胜一次后允许循环继续写。"""
    src = SimpleNamespace(**{f: f"old_{f}" for f in ds.DEATHMATCH_SYNC_FIELDS})
    db = SimpleNamespace(**{f: f"old_{f}" for f in ds.DEATHMATCH_SYNC_FIELDS})
    snap = {f: f"old_{f}" for f in ds.DEATHMATCH_SYNC_FIELDS}
    # 循环本轮改了 status/plan；同时外部动作把 status 改成了 paused、plan 被外部改写
    src.deathmatch_status = "active"
    src.deathmatch_plan = {"steps": [1]}
    db.deathmatch_status = "paused"
    db.deathmatch_plan = {"steps": [9]}
    snap2, frozen = ds.merge_sync_deathmatch_state(db, src, snap, frozenset())
    assert db.deathmatch_status == "paused", "控制字段必须外部获胜"
    assert "deathmatch_status" in frozen, "控制字段冲突后必须冻结"
    assert db.deathmatch_plan == {"steps": [9]}, "数据字段本轮外部获胜"
    # 下一轮循环再改 plan（DB==snapshot）→ 循环写回
    src.deathmatch_plan = {"steps": [2]}
    snap3, frozen2 = ds.merge_sync_deathmatch_state(db, src, snap2, frozen)
    assert db.deathmatch_plan == {"steps": [2]}, "数据字段外部变更一次后循环可继续写"
    # 冻结的控制字段永不写回
    src.deathmatch_status = "done"
    _, frozen3 = ds.merge_sync_deathmatch_state(db, src, snap3, frozen2)
    assert db.deathmatch_status == "paused" and "deathmatch_status" in frozen3


def test_residual_merge_sync_untouched_fields_not_written():
    """循环未触碰的字段绝不写入（外部变更不被盲拷贝覆盖）——旧 last-writer-wins
    行为会把它改掉，本测试即回归锁。"""
    src = SimpleNamespace(**{f: f"loop_{f}" for f in ds.DEATHMATCH_SYNC_FIELDS})
    db = SimpleNamespace(**{f: f"loop_{f}" for f in ds.DEATHMATCH_SYNC_FIELDS})
    snap = {f: f"loop_{f}" for f in ds.DEATHMATCH_SYNC_FIELDS}
    db.deathmatch_plan = {"external": True}
    db.deathmatch_events = [{"type": "external"}]
    ds.merge_sync_deathmatch_state(db, src, snap, frozenset())
    assert db.deathmatch_plan == {"external": True}
    assert db.deathmatch_events == [{"type": "external"}]


def test_residual_merge_sync_dirty_field_written_when_db_matches():
    """无外部写入时，循环脏字段正常落库（不能因合并逻辑丢状态）。"""
    src = SimpleNamespace(**{f: f"new_{f}" for f in ds.DEATHMATCH_SYNC_FIELDS})
    db = SimpleNamespace(**{f: f"old_{f}" for f in ds.DEATHMATCH_SYNC_FIELDS})
    snap = {f: f"old_{f}" for f in ds.DEATHMATCH_SYNC_FIELDS}
    snap2, frozen = ds.merge_sync_deathmatch_state(db, src, snap, frozenset())
    assert db.deathmatch_plan == "new_deathmatch_plan"
    assert db.deathmatch_status == "new_deathmatch_status"
    assert snap2["deathmatch_plan"] == "new_deathmatch_plan"
    assert not frozen


def test_residual_merge_control_external_freeze_even_when_loop_not_dirty():
    """F1 回归锁：外部动作改控制字段时，循环本轮未触碰它也必须冻结，
    否则下一轮 stale 值会被当成「变脏」写回（adopt-then-clobber）。"""
    src = SimpleNamespace(**{f: f"same_{f}" for f in ds.DEATHMATCH_SYNC_FIELDS})
    db = SimpleNamespace(**{f: f"same_{f}" for f in ds.DEATHMATCH_SYNC_FIELDS})
    snap = {f: f"same_{f}" for f in ds.DEATHMATCH_SYNC_FIELDS}
    db.deathmatch_status = "paused"  # 外部动作；循环 status 未变
    snap2, frozen = ds.merge_sync_deathmatch_state(db, src, snap, frozenset())
    assert "deathmatch_status" in frozen
    assert db.deathmatch_status == "paused"
    src.deathmatch_status = "active"  # 下一轮循环的 stale 值
    _, frozen2 = ds.merge_sync_deathmatch_state(db, src, snap2, frozen)
    assert db.deathmatch_status == "paused", "冻结后 stale 值不得写回"
    assert "deathmatch_status" in frozen2


def test_residual_merge_inplace_mutation_detected():
    """F2 回归锁：events/reflections 等原地 append 的字段必须被检测并落库
    （快照深拷贝；旧别名实现会把变更当成「未变脏」而跳过）。
    A4.9 r2 nit: 每次 sync 使用独立的 DB 目标对象（生产语义=每次重载行），
    旧实现下本测试会真实失败（别名使第二次比较误判为未变脏）。"""
    events_src = [{"type": "e1"}]
    src = SimpleNamespace(**{f: f"v_{f}" for f in ds.DEATHMATCH_SYNC_FIELDS})
    src.deathmatch_events = events_src

    def _fresh_db(events):
        db = SimpleNamespace(**{f: f"v_{f}" for f in ds.DEATHMATCH_SYNC_FIELDS})
        db.deathmatch_events = list(events)
        return db

    snap = {f: f"v_{f}" for f in ds.DEATHMATCH_SYNC_FIELDS}
    snap["deathmatch_events"] = [{"type": "e1"}]
    events_src.append({"type": "e2"})  # 模拟 _record_event 原地追加
    db1 = _fresh_db([{"type": "e1"}])
    snap2, _ = ds.merge_sync_deathmatch_state(db1, src, snap, frozenset())
    assert db1.deathmatch_events == [{"type": "e1"}, {"type": "e2"}], (
        "原地追加必须落库（终局批次不得因别名被跳过）"
    )
    events_src.append({"type": "e3"})
    db2 = _fresh_db([{"type": "e1"}, {"type": "e2"}])
    _, _ = ds.merge_sync_deathmatch_state(db2, src, snap2, frozenset())
    assert db2.deathmatch_events[-1] == {"type": "e3"}
