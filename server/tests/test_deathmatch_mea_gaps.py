"""TDD tests: 死磕 MEA 缺口波（LongHorizon-Harness 2608.01964 对照落地）—

G1 judge 增加 blocked/ask 一等控制判词（受阻→可恢复停止；需用户输入→暂停提问，
不再被误判为 DONE）；G2 步骤契约可选 boundary 边界约束字段（schema→continuation
→verifier 透传）；G3 运行时完整性检测（已完成步骤交付物从工作区消失→完整性违规，
阻断完成判定）；T1 轮次标签去除 9999 假总轮数（不限时仅显示当前轮次）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_deathmatch_mea_gaps.py -v
"""
import asyncio
import json
import os
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.deathmatch_service as ds
from app.services.deathmatch_service import DeathmatchManager


# ── fixtures ──────────────────────────────────────────────────────────────

def _conv(**over):
    base = dict(
        id="conv-mea",
        deathmatch_mode=True,
        deathmatch_status="active",
        deathmatch_goal="测试目标",
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
        deathmatch_bible_draft={"x": 1},
        deathmatch_settled_ledger=None,
        deathmatch_subgoals=[],
    )
    base.update(over)
    return SimpleNamespace(**base)


def _mgr(conv, judge=("continue", "not done", False, False, False)):
    mgr = DeathmatchManager(conv)
    mgr.get_continuation_prompt = lambda last_response="": "cont"
    mgr._record_reflection = lambda *a, **k: None
    mgr._record_settled = lambda *a, **k: None
    mgr.collect_final_deliverables_from_messages = lambda *a, **k: asyncio.sleep(0, result=[])
    mgr._maybe_harness_repair = lambda *a, **k: asyncio.sleep(0, result=False)
    # AEWM 波（2609.28416）：停滞分诊也是外部 LLM 缝——同 _maybe_harness_repair
    # 约定伪造（真实调用在单测环境走网络：108s/次且路由非确定）。
    mgr._triage_stall_actions = lambda *a, **k: asyncio.sleep(0, result=None)
    ds._call_judge_llm = lambda goal, resp, **k: asyncio.sleep(0, result=judge)
    return mgr


@pytest.fixture(autouse=True)
def _restore_judge():
    orig = ds._call_judge_llm
    ds._CREATIVE_GOAL_CACHE[ds._normalize_goal_key("测试目标")] = False
    yield
    ds._call_judge_llm = orig
    ds._CREATIVE_GOAL_CACHE.pop(ds._normalize_goal_key("测试目标"), None)


@pytest.fixture
def verify_on():
    # Save/restore the EXACT prior value: popping unconditionally deleted the
    # config.toml-provided verify_enabled=true and broke later tests in the
    # same session (placeholder-gate suite, 2026-09-20).
    dm = ds.config._config.setdefault("deathmatch", {})
    _prev = dm.get("verify_enabled")
    dm["verify_enabled"] = True
    yield
    if _prev is None:
        dm.pop("verify_enabled", None)
    else:
        dm["verify_enabled"] = _prev


@pytest.fixture
def autonomy_off():
    dm = ds.config._config.setdefault("deathmatch", {})
    _prev = dm.get("autonomy_enabled")
    dm["autonomy_enabled"] = False
    yield
    if _prev is None:
        dm.pop("autonomy_enabled", None)
    else:
        dm["autonomy_enabled"] = _prev


def _run(coro):
    return asyncio.run(coro)


# ── T1: 轮次标签仅显示当前轮次（不限时）+ 存量 9999 行启动迁移根治 ─────────

def test_migration_heals_legacy_9999_snapshot():
    """自治波之前会话的 deathmatch_max_turns=9999 假预算快照由启动迁移
    归一为 0（不限）——数据层根治，显示层不认识历史哨兵。"""
    from app.db.migrations import STARTUP_MIGRATIONS
    entries = dict(STARTUP_MIGRATIONS)
    sql = entries.get("conversations_deathmatch_max_turns_legacy_unlimited")
    assert sql is not None, "迁移条目缺失"
    assert "UPDATE conversations" in sql and "deathmatch_max_turns = 0" in sql
    assert "deathmatch_max_turns >= 9999" in sql, "迁移必须只命中旧默认快照（>=9999），不得触碰真实预算"


def test_turn_label_unlimited_shows_current_only():
    conv = _conv(deathmatch_turns=5, deathmatch_max_turns=0)
    prompt = DeathmatchManager(conv).get_continuation_prompt("")
    header = prompt.splitlines()[0]
    assert "第5轮" in header and "不限" not in header and "共" not in header, header


def test_turn_label_real_budget_kept():
    conv = _conv(deathmatch_turns=5, deathmatch_max_turns=30)
    prompt = DeathmatchManager(conv).get_continuation_prompt("")
    assert "第5轮/共30轮" in prompt, "operator-configured budget keeps its total"


# ── G1: judge blocked/ask 一等控制判词 ────────────────────────────────────

def test_judge_parse_blocked_ask():
    v, r, pf, compact = ds._parse_judge_response(
        '{"verdict": "blocked", "reason": "缺少API凭据", "compact": false}')
    assert (v, pf) == ("blocked", False), (v, r, pf)
    v, r, pf, compact = ds._parse_judge_response(
        '{"verdict": "ask", "reason": "需要用户选择导出格式", "compact": false}')
    assert (v, pf) == ("ask", False), (v, r, pf)


def test_blocked_verdict_parks_resumable():
    """blocked = 结构性不可推进：停止循环（自治下也不得无限重置），
    复用 human_gate 可恢复语义；退轮次 + 冻结墙钟（WAIT I1/I2 对齐）。"""
    conv = _conv()
    mgr = _mgr(conv, judge=("blocked", "缺少第三方服务访问凭据", False, False, False))
    mgr.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "partial", "progress": True, "current_step": "s1", "issues": [], "completed_steps": []})
    d = _run(mgr.evaluate_after_turn("我无法继续，因为缺少凭据", workspace_path="/tmp/x"))
    assert d["verdict"] == "blocked" and d["should_continue"] is False, d
    assert d["status"] == "human_gate" and conv.deathmatch_status == "human_gate"
    assert conv.deathmatch_status != "done", "受阻绝不能再被记为完成"
    assert conv.deathmatch_turns == 0, f"blocked 不是自主工作，须退还轮次: {conv.deathmatch_turns}"
    assert conv.deathmatch_wall_time_started_at is None, "墙钟必须冻结"
    assert "缺少第三方服务访问凭据" in (d.get("message") or "")


def test_ask_verdict_parks_with_question():
    conv = _conv()
    mgr = _mgr(conv, judge=("ask", "需要用户拍板导出格式 A 还是 B", False, False, False))
    mgr.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "partial", "progress": True, "current_step": "s1", "issues": [], "completed_steps": []})
    d = _run(mgr.evaluate_after_turn("需要您确认导出格式", workspace_path="/tmp/x"))
    assert d["verdict"] == "ask" and d["should_continue"] is False, d
    assert conv.deathmatch_status == "human_gate"
    assert "导出格式" in (d.get("message") or ""), "问题必须原样浮现给用户"


def test_blocked_ask_also_stop_in_legacy_mode(autonomy_off):
    conv = _conv()
    mgr = _mgr(conv, judge=("blocked", "客观不可推进", False, False, False))
    mgr.verify_step_outputs = lambda lr, wp, tool_results=None: asyncio.sleep(0, result={
        "status": "partial", "progress": True, "current_step": "s1", "issues": [], "completed_steps": []})
    d = _run(mgr.evaluate_after_turn("做不到", workspace_path="/tmp/x"))
    assert d["verdict"] == "blocked" and d["should_continue"] is False


# ── G3: 完整性轴——受保护交付物删除检测 ────────────────────────────────────

_COMPLETE_JSON = json.dumps({
    "status": "complete", "issues": [], "retry_instruction": "",
    "confidence": 0.9, "requires_file": True, "continuity_brief": "x",
}, ensure_ascii=False)


def _integrity_conv():
    return _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "done", "description": "产出报告", "expected_output": "out.txt",
         "verification_method": "", "boundary": "", "output_files": ["out.txt"],
         "output_summary": "报告已产出"},
        {"id": "s2", "status": "in_progress", "description": "产出附录", "expected_output": "app.txt",
         "verification_method": "", "boundary": ""},
    ]})


def _verify_mgr(conv):
    mgr = DeathmatchManager(conv)
    mgr._make_llm = lambda **k: object()
    mgr._llm_generate = lambda *a, **k: asyncio.sleep(0, result=_COMPLETE_JSON)
    return mgr


def test_integrity_deletion_blocks_completion(verify_on):
    """已完成步骤的交付物自上一轮验证后从工作区消失 = 完整性违规：
    LLM 说 complete 也不得定案，结果不得为 complete。"""
    with tempfile.TemporaryDirectory() as ws:
        with open(os.path.join(ws, "out.txt"), "w") as fh:
            fh.write("报告正文" * 40)
        conv = _integrity_conv()
        mgr = _verify_mgr(conv)
        base = _run(mgr.verify_step_outputs("首轮", ws))  # baseline snapshot
        assert base["current_step"] == "s2"
        os.remove(os.path.join(ws, "out.txt"))  # 交付物消失
        with open(os.path.join(ws, "app.txt"), "w") as fh:
            fh.write("附录内容" * 40)
        r = _run(mgr.verify_step_outputs("附录已写好", ws))
    steps = conv.deathmatch_plan["steps"]
    assert steps[1]["status"] != "done", "完整性违规下 LLM 的 complete 不得定案"
    assert r["status"] != "complete", r["status"]
    assert r.get("integrity") == "violation", r.get("integrity")
    assert any("完整性违规" in str(i) and "out.txt" in str(i) for i in r["issues"]), r["issues"]
    # A4.9 I1: 拥有消失交付物的步骤被确定性重开为 pending（计划序重定向恢复），
    # 且从 completed_steps 中移除——不得靠 agent 自觉看到 issue 文本。
    assert steps[0]["status"] == "pending", steps[0]
    assert "s1" not in (r.get("completed_steps") or [])
    assert any("已重开受影响步骤" in str(i) for i in r["issues"]), r["issues"]


def test_integrity_clean_when_deliverable_kept(verify_on):
    with tempfile.TemporaryDirectory() as ws:
        with open(os.path.join(ws, "out.txt"), "w") as fh:
            fh.write("报告正文" * 40)
        conv = _integrity_conv()
        mgr = _verify_mgr(conv)
        _run(mgr.verify_step_outputs("首轮", ws))
        with open(os.path.join(ws, "app.txt"), "w") as fh:
            fh.write("附录内容" * 40)
        r = _run(mgr.verify_step_outputs("附录已写好", ws))
    steps = conv.deathmatch_plan["steps"]
    assert steps[1]["status"] == "done", steps[1]
    assert r.get("integrity") == "clean"
    assert not any("完整性违规" in str(i) for i in r["issues"])


# ── G2: 步骤契约 boundary 字段 ────────────────────────────────────────────

def test_parse_plan_boundary_normalized():
    raw = json.dumps({"steps": [{
        "id": "s1", "description": "分析配置", "expected_output": "分析笔记",
        "verification_method": "", "boundary": "只读分析，严禁修改 config.toml",
    }]}, ensure_ascii=False)
    plan = DeathmatchManager(_conv())._parse_plan(raw)
    assert plan["steps"][0]["boundary"] == "只读分析，严禁修改 config.toml"
    raw2 = json.dumps({"steps": [{
        "id": "s1", "description": "x", "expected_output": "y", "boundary": ["非字符串"],
    }]}, ensure_ascii=False)
    plan2 = DeathmatchManager(_conv())._parse_plan(raw2)
    assert plan2["steps"][0]["boundary"] == "", "非字符串 boundary 必须安全丢弃"


def test_continuation_includes_boundary_block():
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "pending", "description": "分析配置", "expected_output": "笔记",
         "verification_method": "", "boundary": "只读分析，严禁修改 config.toml"},
    ]})
    prompt = DeathmatchManager(conv).get_continuation_prompt("")
    assert "<boundary>" in prompt and "严禁修改 config.toml" in prompt, prompt[:400]


def test_continuation_omits_boundary_block_when_absent():
    prompt = DeathmatchManager(_conv()).get_continuation_prompt("")
    assert "<boundary>" not in prompt


# ── 遗留项收口（A4.9 R2）──────────────────────────────────────────────────

def test_integrity_survives_gate_shortcircuit(verify_on):
    """A1b 验证门禁失败轮次会 early-return——完整性违规必须仍落账
    （violation 标记 + 受损步骤重开 + issue 点名），不得被 gate 短路吞掉。"""
    with tempfile.TemporaryDirectory() as ws:
        with open(os.path.join(ws, "out.txt"), "w") as fh:
            fh.write("报告正文" * 40)
        conv = _integrity_conv()
        conv.deathmatch_plan["steps"][1]["verification_method"] = "gate: false"
        mgr = _verify_mgr(conv)
        mgr._run_verification_gate = lambda wp, step: asyncio.sleep(0, result={
            "status": "partial", "issues": ["验证门禁未通过（exit≠0）"],
            "retry_instruction": "修复后重跑"})
        _run(mgr.verify_step_outputs("首轮", ws))  # baseline
        os.remove(os.path.join(ws, "out.txt"))
        r = _run(mgr.verify_step_outputs("本轮", ws))
    steps = conv.deathmatch_plan["steps"]
    assert r["status"] == "partial" and any("门禁" in str(i) for i in r["issues"]), r
    assert r.get("integrity") == "violation", "gate 短路不得吞掉完整性落账"
    assert any("out.txt" in str(i) for i in r["issues"]), r["issues"]
    assert steps[0]["status"] == "pending", "受损步骤必须仍被重开"
    assert "s1" not in (r.get("completed_steps") or [])


def test_migration_executes_and_heals_live():
    """M3：迁移 SQL 真跑——事务内插 9999/50 两行，执行 STARTUP_MIGRATIONS
    里的迁移原文，断言 9999→0 且 50 不动，ROLLBACK 零副作用。
    DB 不可达时 skip（本机开发库在线时执行）。"""
    asyncpg = pytest.importorskip("asyncpg")
    import tomllib
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.toml")
    with open(cfg_path, "rb") as _fh:
        cfg = tomllib.load(_fh)["database"]
    from app.db.migrations import STARTUP_MIGRATIONS
    sql = dict(STARTUP_MIGRATIONS)["conversations_deathmatch_max_turns_legacy_unlimited"]

    async def _main():
        try:
            conn = await asyncpg.connect(
                host=cfg["host"], port=cfg["port"], user=cfg["username"],
                password=cfg["password"], database=cfg["name"])
        except Exception as exc:
            pytest.skip(f"dev DB unreachable: {exc}")
        try:
            await conn.execute("BEGIN")
            uid = await conn.fetchval("SELECT id FROM users LIMIT 1")
            if not uid:
                pytest.skip("no users in dev DB")
            await conn.execute(
                "INSERT INTO conversations (id, user_id, deathmatch_max_turns) "
                "VALUES ('mea-mig-t1', $1, 9999), ('mea-mig-t2', $1, 50)", uid)
            await conn.execute(sql)  # 迁移原文，非抄写
            healed = await conn.fetchval(
                "SELECT deathmatch_max_turns FROM conversations WHERE id='mea-mig-t1'")
            spared = await conn.fetchval(
                "SELECT deathmatch_max_turns FROM conversations WHERE id='mea-mig-t2'")
            assert healed == 0, f"9999 快照须归一为 0，实得 {healed}"
            assert spared == 50, f"真实预算不得被触碰，实得 {spared}"
        finally:
            await conn.execute("ROLLBACK")
            await conn.close()

    _run(_main())
