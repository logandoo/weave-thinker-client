"""TDD — 论文波次 v2 W2 · D1 证据视图 v1（ACG 2609.32754 需求锚定证据视图）。

交付面（§2.1 + §10 复核勘正 A3/B2/B3 + §10.6 W2 行）：
- 新列 deathmatch_evidence_units（JSON list，默认 []）；A3 勘正：不入
  DEATHMATCH_SYNC_FIELDS（derived state 不同步客户端）。
- 证据单元写点 _record_evidence_unit（与 _record_event 同款 fail-open +
  write_through_bg 写-through）；形状 {id, kind: OBSERVED|FACT|FAIL|CLAIM|
  PATCH_SUMMARY, source: judge|verifier, step_id, text(截 300), ts,
  attributed}；cap 100 保留最新。judge 链在 _build_judge_evidence 产出后
  归档证据摘要；verifier 链在 verify_step_outputs 结束归档 issues/completed
  摘要（续跑链二期，不在 v1）。
- 注入块 _build_evidence_units_block：需求锚定（按当前 step_id 分组）+ 来源
  标注 + 歧义/无归属标「未归因」；块级预算准入——块总长 >
  deathmatch_evidence_units_budget（默认 800）时整块弃换 1 行省略提示
  （不半截截断）。注入面 = _build_judge_evidence 尾部 + verifier 用户提示词。
- 种子 round-robin B2 _seed_evidence_units_round_robin：同一轮内 judge 与
  verifier 两链各得交错种子（对侧链已归档单元轮转抽样 ≤3 条交叉注入），
  纯确定性。
- 开关 deathmatch_evidence_units_enabled 默认 False；关时注入块为空、写点
  归档照记（**归档不注入**——本文件钉死该语义）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_deathmatch_evidence_units.py -v
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.deathmatch_service as ds
from app.services.deathmatch_service import DEATHMATCH_SYNC_FIELDS, DeathmatchManager


def _conv(**over):
    base = dict(
        id="conv-eunits",
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
        deathmatch_evidence_units=[],
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _creative_cache():
    key = ds._normalize_goal_key("测试目标")
    ds._CREATIVE_GOAL_CACHE[key] = False
    yield
    ds._CREATIVE_GOAL_CACHE.pop(key, None)


@pytest.fixture
def _eunits_on():
    """W2 v1 开关：开启注入 + 放大预算（预算准入用例自行收紧）。"""
    dm = ds.config._config.setdefault("deathmatch", {})
    p1 = dm.pop("evidence_units_enabled", "___absent___")
    p2 = dm.pop("evidence_units_budget", "___absent___")
    dm["evidence_units_enabled"] = True
    dm["evidence_units_budget"] = 100000
    yield dm
    for k, p in (("evidence_units_enabled", p1), ("evidence_units_budget", p2)):
        if p == "___absent___":
            dm.pop(k, None)
        else:
            dm[k] = p


def _run(coro):
    return asyncio.run(coro)


def _mgr(conv):
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return json.dumps({"status": "partial", "issues": ["缺少实测数据"],
                           "retry_instruction": "", "confidence": 0.4,
                           "continuity_brief": "", "requires_file": True},
                          ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    mgr._record_settled = lambda *a, **k: None
    return mgr


# ════════════ 1. 单元归档形状 / 截 300 / cap 200 保留最新 ════════════

def test_record_evidence_unit_shape_truncate_and_cap():
    conv = _conv()
    mgr = DeathmatchManager(conv)
    u = mgr._record_evidence_unit("OBSERVED", "judge", "x" * 400, step_id="")
    assert u is not None
    assert set(u.keys()) == {"id", "kind", "source", "step_id", "text", "ts",
                            "attributed", "taint"}
    assert u["kind"] == "OBSERVED"
    assert u["source"] == "judge"
    assert u["step_id"] == ""
    assert u["attributed"] is False, "无 step_id（无归属）必须保持未归因"
    assert u["text"].endswith("[截断]") and len(u["text"]) <= 308, "text 必须截 300"
    assert isinstance(u["ts"], float)
    assert u["id"]
    assert conv.deathmatch_evidence_units == [u]

    u2 = mgr._record_evidence_unit("FACT", "verifier", "完成", step_id="s1")
    assert u2["attributed"] is True, "已校验来源声明（step_id）→ 归因"
    assert conv.deathmatch_evidence_units == [u, u2]

    # 非法 kind 归一 OBSERVED（fail-open，不抛）
    u3 = mgr._record_evidence_unit("WEIRD", "judge", "t", step_id="")
    assert u3["kind"] == "OBSERVED"
    # 显式歧义：有 step_id 但归因声明为歧义 → 保持未归因
    u4 = mgr._record_evidence_unit("CLAIM", "verifier", "t", step_id="s1", attributed=False)
    assert u4["attributed"] is False

    # cap 200 保留最新（fix-round A3：100→200）
    for i in range(220):
        mgr._record_evidence_unit("OBSERVED", "judge", f"n{i}", step_id="")
    units = conv.deathmatch_evidence_units
    assert len(units) == 200, "cap 200"
    assert units[-1]["text"] == "n219", "保留最新"
    assert all(u["text"] != "n0" for u in units), "最旧的必须被淘汰"


def test_record_evidence_unit_unknown_source_marked_unknown():
    """fix-round Minors：未知来源标 "unknown"（不得默认冒充 judge）。"""
    conv = _conv()
    mgr = DeathmatchManager(conv)
    u = mgr._record_evidence_unit("FACT", "tool_results", "拿到实测输出", step_id="s1")
    assert u["source"] == "unknown", "未知来源必须标 unknown"
    u2 = mgr._record_evidence_unit("FACT", "", "t", step_id="")
    assert u2["source"] == "unknown", "空来源也必须标 unknown"


# ════════════ 2. 块渲染：需求锚定分组 + 来源标注 + 未归因 ════════════

def test_evidence_block_groups_by_step_with_source_and_unattributed(_eunits_on):
    conv = _conv()
    mgr = DeathmatchManager(conv)
    mgr._record_evidence_unit("FACT", "verifier", "V1 已完成产出", step_id="s1")
    mgr._record_evidence_unit("FAIL", "verifier", "V0 前序问题", step_id="s0")
    mgr._record_evidence_unit("OBSERVED", "judge", "J1 跨步骤证据包", step_id="")
    block = mgr._build_evidence_units_block("verifier", current_step_id="s1")
    assert block.startswith("<evidence_units>") and block.endswith("</evidence_units>")
    assert "V1 已完成产出" in block and "V0 前序问题" in block
    assert "J1 跨步骤证据包" in block
    assert "verifier" in block and "judge" in block, "必须来源标注"
    assert "未归因" in block, "歧义/无归属必须标「未归因」"
    assert "步骤 s1（当前）" in block, "按当前 step_id 分组"
    assert "步骤 s0" in block
    # 归因分组序：当前步 → 其他步 → 未归因
    assert block.index("步骤 s1（当前）") < block.index("步骤 s0") < block.index("未归因")


# ════════════ 3. 块级预算准入：超预算整块弃（不半截截断） ════════════

def test_evidence_block_budget_admission_drops_whole_block(_eunits_on):
    conv = _conv()
    mgr = DeathmatchManager(conv)
    mgr._record_evidence_unit("OBSERVED", "judge", "AAA" * 100, step_id="")
    mgr._record_evidence_unit("FACT", "verifier", "BBB" * 100, step_id="s1")
    # 收紧预算到必然超限
    ds.config._config["deathmatch"]["evidence_units_budget"] = 80
    block = mgr._build_evidence_units_block("judge", current_step_id="s1")
    assert "AAA" not in block and "BBB" not in block, "超预算必须整块弃，不得半截截断"
    assert "整块省略" in block, "必须换 1 行省略提示"
    assert "\n" not in block, "省略提示必须是 1 行"
    assert block.startswith("<evidence_units>") and block.endswith("</evidence_units>")
    # 预算恢复后整块入
    ds.config._config["deathmatch"]["evidence_units_budget"] = 100000
    block2 = mgr._build_evidence_units_block("judge", current_step_id="s1")
    assert "AAA" in block2 and "BBB" in block2


def test_evidence_block_budget_zero_means_no_cap(_eunits_on):
    """fix-round Minors：`or 800` 改显式 is None 判断——0=无预算（不设上限，
    预算检查跳过）；0 不得被当成缺省卷回 800。
    判别形状：块长 >800（卷回 800 会整块弃；0=无预算则整块入）。"""
    conv = _conv()
    mgr = DeathmatchManager(conv)
    for i in range(3):
        mgr._record_evidence_unit("OBSERVED", "judge", f"{i}" + "A" * 300, step_id="")
    ds.config._config["deathmatch"]["evidence_units_budget"] = 0
    block = mgr._build_evidence_units_block("judge", current_step_id="s1")
    assert len(block) > 800, "判别前提：块长必须 >800"
    assert "AAA" in block, "0=无预算（不设上限）——不得卷回 800 触发整块弃"


def test_evidence_units_budget_none_falls_back_800(_eunits_on):
    """fix-round Minors：显式 None = 缺省 → 800（is None 判断）。"""
    ds.config._config["deathmatch"]["evidence_units_budget"] = None
    from app.core.config import get_config
    assert get_config().deathmatch_evidence_units_budget == 800


# ════════════ 4. 种子 round-robin B2：对侧链 ≤3 条交错注入（纯确定性） ══

def test_seed_round_robin_cross_injects_le3_interleaved(_eunits_on):
    conv = _conv(deathmatch_turns=0)
    mgr = DeathmatchManager(conv)
    # 交替归档 → ts 序交错
    mgr._record_evidence_unit("OBSERVED", "judge", "J0", step_id="")
    mgr._record_evidence_unit("OBSERVED", "verifier", "V0", step_id="s1")
    mgr._record_evidence_unit("OBSERVED", "judge", "J1", step_id="")
    mgr._record_evidence_unit("OBSERVED", "verifier", "V1", step_id="s1")
    mgr._record_evidence_unit("OBSERVED", "judge", "J2", step_id="")
    mgr._record_evidence_unit("OBSERVED", "verifier", "V2", step_id="s1")
    mgr._record_evidence_unit("OBSERVED", "verifier", "V3", step_id="s1")
    mgr._record_evidence_unit("OBSERVED", "verifier", "V4", step_id="s1")

    b_judge = mgr._build_evidence_units_block("judge", current_step_id="s1")
    seed_lines = [ln for ln in b_judge.splitlines() if "|verifier]" in ln]
    assert len(seed_lines) == 3, "judge 块交叉注入对侧链单元 ≤3 条"
    assert "J0" in b_judge and "J1" in b_judge and "J2" in b_judge
    assert b_judge.index("V0") < b_judge.index("J1"), "种子必须交错入列（非尾部追加）"

    b_verifier = mgr._build_evidence_units_block("verifier", current_step_id="s1")
    seed_lines_v = [ln for ln in b_verifier.splitlines() if "|judge]" in ln]
    assert len(seed_lines_v) == 3, "verifier 块交叉注入对侧链单元 ≤3 条"

    # 纯确定性：同输入同输出
    assert mgr._build_evidence_units_block("judge", current_step_id="s1") == b_judge

    # 轮转：turns 移位 → 种子窗口不同
    conv.deathmatch_turns = 1
    b_rot = mgr._build_evidence_units_block("judge", current_step_id="s1")
    picked_0 = {ln.split("] ", 1)[1] for ln in b_judge.splitlines() if "|verifier]" in ln}
    picked_1 = {ln.split("] ", 1)[1] for ln in b_rot.splitlines() if "|verifier]" in ln}
    assert picked_0 != picked_1, "轮转抽样必须随轮次移位"


# ════════════ 5. 开关默认关：注入为空 + 归档照记（归档不注入） ════════════

def test_switch_off_injection_empty_but_archive_still_lands():
    dm = ds.config._config.setdefault("deathmatch", {})
    dm.pop("evidence_units_enabled", None)
    assert ds.config.deathmatch_evidence_units_enabled is False, "默认必须关"
    conv = _conv()
    mgr = DeathmatchManager(conv)
    u = mgr._record_evidence_unit("OBSERVED", "judge", "J", step_id="")
    assert u is not None and conv.deathmatch_evidence_units == [u], "关闭时归档照记"
    assert mgr._build_evidence_units_block("judge") == "", "关闭时注入块必须为空"


# ════════════ 6. 新列不入 DEATHMATCH_SYNC_FIELDS（A3 勘正） ════════════

def test_evidence_units_column_not_in_sync_fields():
    assert "deathmatch_evidence_units" not in DEATHMATCH_SYNC_FIELDS, (
        "A3 勘正：derived state 不同步客户端")
    assert len(DEATHMATCH_SYNC_FIELDS) == 28, "同步面不得被本波扩大"
    from app.db.database import Conversation
    cols = {c.name for c in Conversation.__table__.columns}
    assert "deathmatch_evidence_units" in cols, "新列必须是 Conversation 列"


# ════════════ 7. 迁移 SQL + 落位（非 memory，pgvector 标记之前） ════════════

def test_evidence_units_migration_sql_and_placement():
    from app.db.migrations import _MEMORY_MIGRATION_START, STARTUP_MIGRATIONS
    stmt = dict(STARTUP_MIGRATIONS)["conversations_deathmatch_evidence_units"]
    assert "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS " \
           "deathmatch_evidence_units JSONB DEFAULT '[]'::jsonb" == stmt
    names = [v for v, _ in STARTUP_MIGRATIONS]
    assert names.index("conversations_deathmatch_evidence_units") < names.index(
        _MEMORY_MIGRATION_START), "非 memory 迁移必须置于 pgvector 跳过窗之前"


# ════════════ 8. 写-through 落库（sqlite shim，仿 write_through 套件） ══

def test_write_through_persists_evidence_units(monkeypatch):
    from sqlalchemy import BigInteger, create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    @compiles(JSONB, "sqlite")
    def _jsonb(element, compiler, **kw):
        return "JSON"

    @compiles(BigInteger, "sqlite")
    def _bi(element, compiler, **kw):
        return "INTEGER"

    from app.db.database import Base, Conversation, User
    from app.services.deathmatch_write_through import drain_write_through

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__, Conversation.__table__])
    with Session(engine) as s:
        u = User(username="eu", password_hash="x")
        s.add(u)
        s.flush()
        c = Conversation(user_id=u.id, title="t", deathmatch_status="active")
        s.add(c)
        s.commit()
        cid = c.id

    class _AsyncShim:
        def __init__(self, session):
            self._s = session

        async def execute(self, stmt):
            return self._s.execute(stmt)

        async def commit(self):
            return self._s.commit()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            self._s.close()
            return False

    import app.db.database as dbm

    monkeypatch.setattr(dbm, "AsyncSessionLocal", lambda: _AsyncShim(Session(engine)))

    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        s.expunge(conv)  # detached（等价 task_db.close 后的运行面）
    mgr = DeathmatchManager(conv)

    async def go():
        mgr._record_evidence_unit("OBSERVED", "judge", "写-through 单元", step_id="")
        await drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        units = row.deathmatch_evidence_units or []
        assert len(units) == 1, "写-through 必须把证据单元落库"
        assert units[0]["text"] == "写-through 单元"
        assert units[0]["source"] == "judge"


# ════════════ 9. judge 链：产出后归档 + 尾部注入（续跑链不在 v1） ════════════

def test_judge_chain_archives_after_production_and_injects_tail(_eunits_on, tmp_path):
    ws = str(tmp_path)
    (tmp_path / "a.txt").write_text("内容" * 80, encoding="utf-8")
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s0", "status": "done", "description": "前序",
         "output_summary": "前序产出"},
        {"id": "s1", "status": "in_progress", "description": "步骤1",
         "expected_output": "deliverable.md"},
    ]})
    mgr = DeathmatchManager(conv)
    mgr._record_evidence_unit("FAIL", "verifier", "V-种子", step_id="s1")
    ev = mgr._build_judge_evidence(ws)
    units = conv.deathmatch_evidence_units
    assert len(units) == 2, "judge 链必须在 _build_judge_evidence 产出后归档"
    ju = units[-1]
    assert ju["source"] == "judge" and ju["kind"] == "OBSERVED"
    assert ju["attributed"] is False, "跨步骤证据包歧义 → 保持未归因"
    assert len(ju["text"]) <= 308, "证据摘要 text 截 300"
    assert ev.rstrip().endswith("</evidence_units>"), "证据单元块必须注入在尾部"
    assert "<evidence_units>" in ev and "V-种子" in ev, "块内必须交叉注入对侧链种子"
    # 第二次调用：产出后归档的摘要进入下一次渲染（需求锚定视图可见）
    ev2 = mgr._build_judge_evidence(ws)
    assert ju["text"] in ev2, "归档单元必须进入后续证据视图"


# ════════════ 10. verifier 链：结束归档 issues/completed + 提示词注入 ══════

def test_verifier_chain_archives_issues_and_injects_prompt(_eunits_on, tmp_path):
    ws = str(tmp_path)
    conv = _conv()
    mgr = DeathmatchManager(conv)
    mgr._record_evidence_unit("OBSERVED", "judge", "J-种子", step_id="")
    seen = {}

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        seen["prompt"] = user_prompt
        return json.dumps({"status": "partial", "issues": ["缺少实测数据"],
                           "retry_instruction": "", "confidence": 0.4,
                           "continuity_brief": "", "requires_file": True},
                          ensure_ascii=False)

    mgr._llm_generate = fake_gen
    conv.deathmatch_last_verification_result = {
        "status": "partial", "workspace_files": [], "completed_steps": [],
    }
    res = _run(mgr.verify_step_outputs("进行中", ws))
    assert "<evidence_units>" in seen["prompt"], "verifier 用户提示词必须注入证据单元块"
    assert "J-种子" in seen["prompt"], "对侧链种子必须交叉注入"
    units = conv.deathmatch_evidence_units
    fails = [u for u in units if u["kind"] == "FAIL" and u["source"] == "verifier"]
    assert fails, "verifier 链必须在 verify_step_outputs 结束归档 issues 摘要"
    fu = fails[-1]
    assert "缺少实测数据" in fu["text"]
    assert fu["step_id"] == "s1" and fu["attributed"] is True, (
        "归因只认已校验来源声明（本链验证的当前步骤）")
    assert res["status"] == "partial"


def test_verifier_chain_archives_completed_summary(_eunits_on, tmp_path):
    ws = str(tmp_path)
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "步骤1",
         "expected_output": "deliverable.md", "output_files": ["deliverable.md"]},
    ]})
    mgr = DeathmatchManager(conv)
    conv.deathmatch_last_verification_result = {
        "status": "partial", "workspace_files": [], "completed_steps": [],
    }
    (tmp_path / "deliverable.md").write_text("正文" * 80, encoding="utf-8")  # 本轮新产出

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return json.dumps({"status": "complete", "issues": [],
                           "retry_instruction": "", "confidence": 0.9,
                           "continuity_brief": "", "requires_file": True},
                          ensure_ascii=False)

    mgr._llm_generate = fake_gen
    _run(mgr.verify_step_outputs("已完成", ws))
    units = conv.deathmatch_evidence_units
    facts = [u for u in units if u["kind"] == "FACT" and u["source"] == "verifier"]
    assert facts, "verifier 链必须归档 completed 摘要"
    assert "s1" in facts[-1]["text"]


# ════════════ 11. 配置面：开关默认关 / 预算默认 800 ════════════

def test_config_defaults_off_and_budget_800():
    dm = ds.config._config.setdefault("deathmatch", {})
    p1 = dm.pop("evidence_units_enabled", "___absent___")
    p2 = dm.pop("evidence_units_budget", "___absent___")
    try:
        assert ds.config.deathmatch_evidence_units_enabled is False
        assert ds.config.deathmatch_evidence_units_budget == 800
    finally:
        for k, p in (("evidence_units_enabled", p1), ("evidence_units_budget", p2)):
            if p == "___absent___":
                dm.pop(k, None)
            else:
                dm[k] = p
