"""TDD — 遗留清偿：B-8 写-through（D-15）/ A-N1 基线快照出处（D-17）/ A-N2 local_patch 事件。

B-8：步骤状态/事件/台账/反思写点做**目标列 UPDATE 写-through**（绝不触碰
控制字段），断开每-verdict merge 后状态仍落库。
A-N1：出处口径=基线快照成员——目标环内产出（含 unzip/tar 保 mtime 产物）过闸，
目标启动前遗留（基线成员）不过闸。
A-N2：local_patch 替换迁移事件不再缺席（pending→pending 亦记）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_deathmatch_write_through_provenance.py -v
"""
import asyncio
import copy
import os
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.deathmatch_service as ds
from app.services.deathmatch_service import DeathmatchManager
from app.services.deathmatch_write_through import drain_write_through


@compiles(JSONB, "sqlite")
def _jsonb(element, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bi(element, compiler, **kw):
    return "INTEGER"


def _env():
    from app.db.database import Base, Conversation, User

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__, Conversation.__table__])
    with Session(engine) as s:
        u = User(username="wt", password_hash="x")
        s.add(u)
        s.flush()
        c = Conversation(user_id=u.id, title="t",
                         deathmatch_plan={"steps": [{"id": "s1", "status": "pending"}]},
                         deathmatch_plan_version=1, deathmatch_status="active")
        s.add(c)
        s.commit()
        return engine, c.id


def _run(coro):
    return asyncio.run(coro)


class _AsyncShim:
    """sync sqlite Session → 写-through 期待的 async 接口（仿 test_stream_persistence）。"""

    def __init__(self, session):
        self._s = session

    async def execute(self, stmt):
        return self._s.execute(stmt)

    async def commit(self):
        return self._s.commit()

    async def get(self, model, ident):
        return self._s.get(model, ident)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self._s.close()
        return False


def _patch_factory(monkeypatch, engine):
    import app.db.database as dbm

    def factory():
        return _AsyncShim(Session(engine))

    monkeypatch.setattr(dbm, "AsyncSessionLocal", factory)
    import app.api.chat as chat
    import app.api.chat_stream_transport as transport
    monkeypatch.setattr(chat, "AsyncSessionLocal", factory)
    monkeypatch.setattr(transport, "AsyncSessionLocal", factory, raising=False)


# ── B-8：写-through（不依赖 merge 通道；不碰控制字段） ───────────────────

def test_step_transition_write_through_without_merge(monkeypatch):
    """B-8 核心：detached conv 上的状态迁移经写-through 目标列落库
    （本测试不跑任何 merge 通道）。"""
    from app.db.database import Conversation
    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        s.expunge(conv)  # detached（等价 task_db.close 后的运行面）
    mgr = DeathmatchManager(conv)

    async def go():
        mgr._set_step_status(conv.deathmatch_plan["steps"][0], "done", "unit")
        await drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_plan["steps"][0]["status"] == "done", (
            "写-through 必须在无 merge 通道时落库（B-8/D-15）")


def test_write_through_never_touches_control_fields(monkeypatch):
    from app.db.database import Conversation
    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        conv.deathmatch_status = "paused"  # 控制字段：外部（用户）语义
        s.commit()
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        s.expunge(conv)
        conv.deathmatch_status = "active"  # 内存态陈旧/篡改——写-through 不得写它
        mgr = DeathmatchManager(conv)

        async def go():
            mgr._set_step_status(conv.deathmatch_plan["steps"][0], "in_progress", "unit")
            await drain_write_through()

        _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_status == "paused", "写-through 只写数据列，控制字段不可触碰"
        assert row.deathmatch_plan["steps"][0]["status"] == "in_progress"


def test_event_settled_reflection_write_through(monkeypatch):
    from app.db.database import Conversation
    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        s.expunge(conv)
    mgr = DeathmatchManager(conv)
    from app.services.deathmatch_reflection import ReflectionMemory
    mem = ReflectionMemory(conv)

    async def go():
        mgr._record_event("unit_event", note="x")
        mgr._record_settled({"type": "step_complete", "summary": "s"})
        mem.add(turn=1, action_summary="a", verdict="partial", issues=["i"])
        await drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert any(e.get("type") == "unit_event" for e in (row.deathmatch_events or []))
        assert any(e.get("type") == "step_complete" for e in (row.deathmatch_settled_ledger or []))
        assert (row.deathmatch_reflections or []), "反思写-through 必须落库"


def test_replan_persists_plan_obligations_and_epoch_together(monkeypatch):
    """Fresh-goal replan must atomically persist plan, criteria and QA epoch."""
    from app.db.database import Conversation

    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        conv.deathmatch_goal = "执行真实计算并写结果"
        conv.deathmatch_plan = None
        conv.deathmatch_plan_version = 4
        conv.deathmatch_acceptance_criteria = [{
            "id": "c1", "text": "基础标准", "type": "judgmental",
            "source": "system", "check": {"kind": "none"},
        }]
        conv.deathmatch_grilling_qa_history = [{
            "goal_contract_pending": True, "plan_version": 4,
        }]
        s.commit()
        s.refresh(conv)
        s.expunge(conv)
    mgr = DeathmatchManager(conv)
    mgr._make_llm = lambda **kwargs: object()

    async def fake_generate(*args, **kwargs):
        return ""

    async def fake_parse(*args, **kwargs):
        return {"steps": [{
            "id": "s1", "status": "pending", "description": "计算",
            "expected_output": "result.txt", "output_files": ["result.txt"],
        }]}

    async def fake_steps(*args, **kwargs):
        return set()

    async def fake_need(_goal):
        return True

    mgr._llm_generate = fake_generate
    mgr._parse_plan_with_repair = fake_parse
    mgr._judge_steps_needing_evidence_spec = fake_steps
    mgr._obligation_criteria = lambda: [{
        "id": "ob1", "obligation": True, "text": "真实执行义务",
        "type": "mechanical", "source": "system",
        "check": {"kind": "execution_content", "paths": [], "allow": False},
    }]
    monkeypatch.setattr("app.services.deathmatch_plan._ensure_execution_judged", fake_need)

    async def go():
        assert await mgr.replan({"status": "partial", "issues": [], "retry_instruction": ""}) is not None
        await drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_plan is not None
        assert any(c.get("obligation") for c in (row.deathmatch_acceptance_criteria or []))
        pending = [e for e in (row.deathmatch_grilling_qa_history or []) if "goal_contract_pending" in e]
        assert pending and pending[-1]["goal_contract_pending"] is False


def test_every_plan_write_carries_matching_contract_and_epoch(monkeypatch):
    """Queued plan-only writes must not publish a mismatched live contract."""
    from app.db.database import Conversation
    import app.services.deathmatch_write_through as wt

    engine, cid = _env()
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        s.expunge(conv)
    conv.deathmatch_acceptance_criteria = [{
        "id": "ob1", "obligation": True, "text": "真实执行义务",
        "type": "mechanical", "source": "system",
        "check": {"kind": "execution_content", "paths": [], "allow": False},
    }]
    conv.deathmatch_grilling_qa_history = [{"goal_contract_pending": False}]
    captured = []

    async def fake_write(_cid, columns, **_kwargs):
        captured.append(columns)

    monkeypatch.setattr(wt, "_write_columns", fake_write)

    async def go():
        wt.write_through_bg(
            conv,
            deathmatch_plan=conv.deathmatch_plan,
            deathmatch_plan_version=conv.deathmatch_plan_version,
        )
        await wt.drain_write_through()

    _run(go())
    assert captured
    assert captured[-1]["deathmatch_acceptance_criteria"] == conv.deathmatch_acceptance_criteria
    assert captured[-1]["deathmatch_grilling_qa_history"] == conv.deathmatch_grilling_qa_history


def test_stale_detached_plan_write_cannot_overwrite_newer_goal(monkeypatch):
    """A queued old-goal write must lose against a committed resume correction."""
    from app.db.database import Conversation
    import app.services.deathmatch_write_through as wt

    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        conv.deathmatch_goal = "旧目标"
        conv.deathmatch_acceptance_criteria = [{
            "id": "old", "text": "旧合同", "type": "judgmental",
            "source": "system", "check": {"kind": "none"},
        }]
        conv.deathmatch_grilling_qa_history = []
        s.commit()
        s.refresh(conv)
        s.expunge(conv)

    async def go():
        wt.write_through_bg(
            conv,
            deathmatch_plan=conv.deathmatch_plan,
            deathmatch_plan_version=conv.deathmatch_plan_version,
            deathmatch_acceptance_criteria=conv.deathmatch_acceptance_criteria,
            deathmatch_grilling_qa_history=conv.deathmatch_grilling_qa_history,
        )
        # Commit object B's correction before the queued object-A task can run.
        with Session(engine) as s:
            newer = s.get(Conversation, cid)
            newer.deathmatch_goal = "新目标：不要用 gate，改用 exact_file"
            newer.deathmatch_plan = None
            newer.deathmatch_plan_version += 1
            newer.deathmatch_acceptance_criteria = None
            newer.deathmatch_grilling_qa_history = [{
                "resume_correction": "不要用 gate，改用 exact_file",
            }, {"goal_contract_pending": True}]
            s.commit()
        await wt.drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_goal == "新目标：不要用 gate，改用 exact_file"
        assert row.deathmatch_plan is None
        assert row.deathmatch_acceptance_criteria is None
        assert row.deathmatch_grilling_qa_history[-1]["goal_contract_pending"] is True


def test_stale_settled_write_cannot_overwrite_newer_goal_epoch(monkeypatch):
    """Ledger-only writes need the same goal/version optimistic guard."""
    from app.db.database import Conversation
    import app.services.deathmatch_write_through as wt

    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        conv.deathmatch_goal = "同一目标"
        conv.deathmatch_plan_version = 1
        conv.deathmatch_settled_ledger = [{"step_id": "old", "verdict": "done"}]
        s.commit()
        s.refresh(conv)
        s.expunge(conv)

    async def go():
        wt.write_through_bg(
            conv,
            deathmatch_settled_ledger=conv.deathmatch_settled_ledger,
        )
        with Session(engine) as s:
            newer = s.get(Conversation, cid)
            newer.deathmatch_plan_version = 2
            newer.deathmatch_settled_ledger = None
            newer.deathmatch_grilling_qa_history = [{
                "resume_correction": "新增实质纠正",
            }, {"goal_contract_pending": True}]
            s.commit()
        await wt.drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_goal == "同一目标"
        assert row.deathmatch_settled_ledger is None


def test_same_goal_equal_version_aba_cannot_restore_stale_contract(monkeypatch):
    """A stale 1→2 writer must not overwrite B's committed 1→2 contract."""
    from app.db.database import Conversation
    from app.services.deathmatch_service import DeathmatchManager
    import app.services.deathmatch_write_through as wt

    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    with Session(engine) as s:
        stale = s.get(Conversation, cid)
        stale.deathmatch_goal = "同一目标"
        stale.deathmatch_plan_version = 1
        stale.deathmatch_plan = {"steps": [{"id": "old", "status": "pending"}]}
        stale.deathmatch_acceptance_criteria = [{"id": "old", "text": "旧合同"}]
        stale.deathmatch_grilling_qa_history = []
        s.commit()
        s.refresh(stale)
        s.expunge(stale)

    with Session(engine) as s:
        newer = s.get(Conversation, cid)
        newer.deathmatch_plan_version = 2
        newer.deathmatch_plan = {"steps": [{"id": "new", "status": "pending"}]}
        newer.deathmatch_acceptance_criteria = [{"id": "new", "text": "新合同"}]
        newer.deathmatch_grilling_qa_history = [{"goal_contract_pending": True}]
        s.commit()

    async def go():
        mgr = DeathmatchManager(stale)
        mgr._record_event("unrelated_event_warmup")
        step = stale.deathmatch_plan["steps"][0]
        mgr._set_step_status(step, "done", "stale_writer")
        await wt.drain_write_through()
        # A rejected stale writer must stay rejected on later transitions too.
        stale.deathmatch_plan_version = 3
        wt.write_through_bg(
            stale,
            _expected_previous_plan_version=2,
            deathmatch_plan=stale.deathmatch_plan,
            deathmatch_plan_version=3,
            deathmatch_acceptance_criteria=stale.deathmatch_acceptance_criteria,
            deathmatch_grilling_qa_history=stale.deathmatch_grilling_qa_history,
        )
        await wt.drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_plan["steps"][0]["id"] == "new"
        assert row.deathmatch_acceptance_criteria == [{"id": "new", "text": "新合同"}]
        assert row.deathmatch_grilling_qa_history[-1]["goal_contract_pending"] is True


def test_same_version_detached_writers_cannot_aba_restore_old_plan(monkeypatch):
    from app.db.database import Conversation
    import app.services.deathmatch_write_through as wt

    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    with Session(engine) as s:
        stale = s.get(Conversation, cid)
        stale.deathmatch_goal = "same"
        stale.deathmatch_plan_version = 1
        stale.deathmatch_plan = {"steps": [{"id": "stale"}]}
        stale.deathmatch_acceptance_criteria = [{"id": "stale"}]
        stale.deathmatch_grilling_qa_history = []
        s.commit()
        s.refresh(stale)
        s.expunge(stale)
    with Session(engine) as s:
        newer = s.get(Conversation, cid)
        newer.deathmatch_plan = {"steps": [{"id": "new"}]}
        newer.deathmatch_acceptance_criteria = [{"id": "new"}]
        newer.deathmatch_grilling_qa_history = [{"goal_contract_pending": True}]
        s.commit()

    async def go():
        wt.write_through_bg(
            newer,
            deathmatch_plan=newer.__dict__.get("deathmatch_plan"),
            deathmatch_acceptance_criteria=newer.__dict__.get("deathmatch_acceptance_criteria"),
            deathmatch_grilling_qa_history=newer.__dict__.get("deathmatch_grilling_qa_history"),
        )
        wt.write_through_bg(
            stale,
            deathmatch_plan=stale.__dict__.get("deathmatch_plan"),
            deathmatch_acceptance_criteria=stale.__dict__.get("deathmatch_acceptance_criteria"),
            deathmatch_grilling_qa_history=stale.__dict__.get("deathmatch_grilling_qa_history"),
        )
        await wt.drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_plan["steps"][0]["id"] == "new"
        assert row.deathmatch_acceptance_criteria == [{"id": "new"}]
        assert row.deathmatch_grilling_qa_history[-1]["goal_contract_pending"] is True


def test_normal_detached_transition_and_settled_both_persist(monkeypatch):
    from app.db.database import Conversation
    from app.services.deathmatch_service import DeathmatchManager
    import app.services.deathmatch_write_through as wt

    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        s.refresh(conv)
        s.expunge(conv)

    async def go():
        mgr = DeathmatchManager(conv)
        step = conv.deathmatch_plan["steps"][0]
        mgr._set_step_status(step, "done", "normal_completion")
        mgr._record_settled({"type": "step_complete", "step": step})
        await wt.drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_plan_version == 2
        assert row.deathmatch_plan["steps"][0]["status"] == "done"
        assert row.deathmatch_settled_ledger, "正常 transition 后的 settled 不得被自身 FIFO CAS 拒绝"


def test_production_rebind_initializes_cas_and_preserves_settled(monkeypatch):
    from app.db.database import Conversation
    from app.api.chat_stream_transport import _rebind_deathmatch_manager_conversation
    from app.services.deathmatch_service import DeathmatchManager
    import app.services.deathmatch_write_through as wt

    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    with Session(engine) as s:
        fresh = s.get(Conversation, cid)
        s.refresh(fresh)
        s.expunge(fresh)
    mgr = DeathmatchManager(SimpleNamespace(
        id="other-object", deathmatch_goal=None, deathmatch_plan_version=0,
        deathmatch_plan=None, deathmatch_acceptance_criteria=None,
        deathmatch_grilling_qa_history=[], deathmatch_settled_ledger=None,
    ))

    async def go():
        _rebind_deathmatch_manager_conversation(mgr, fresh)
        step = fresh.deathmatch_plan["steps"][0]
        mgr._set_step_status(step, "done", "production_rebind")
        mgr._record_settled({"type": "step_complete", "step": step})
        await wt.drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_plan["steps"][0]["status"] == "done"
        assert row.deathmatch_settled_ledger, "rebind 后正常 settled 不得被旧基线拒绝"


def test_own_verdict_merge_rebases_later_write_through(monkeypatch):
    from app.db.database import Conversation
    import app.api.chat_stream_transport as transport
    from app.api.chat_stream_transport import _handle_dm_verdict_event, _rebind_deathmatch_manager_conversation
    from app.services.deathmatch_service import DEATHMATCH_SYNC_FIELDS
    import app.services.deathmatch_write_through as wt

    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    transport._sync_chat_globals()
    with Session(engine) as s:
        fresh = s.get(Conversation, cid)
        s.refresh(fresh)
        snapshot = {f: copy.deepcopy(getattr(fresh, f)) for f in DEATHMATCH_SYNC_FIELDS}
        s.expunge(fresh)
    mgr = DeathmatchManager(SimpleNamespace(id="other", deathmatch_plan=None))
    _rebind_deathmatch_manager_conversation(mgr, fresh)

    async def go():
        fresh.deathmatch_plan = {"steps": [{"id": "s1", "status": "in_progress"}], "v": 2}
        fresh.deathmatch_plan_version = 2
        async def put(_event):
            return None
        run = SimpleNamespace(
            conversation_id=cid, task_conversation=fresh,
            dm_sync_snapshot=snapshot, dm_sync_frozen=frozenset(),
            dm_final_attachments=[], assistant_content="", content_segments=[],
            display_sequence=[], tool_results_accumulated=[], search_queries_used=[],
            search_queries_by_call={}, tool_call_events_accumulated=[],
            pre_tool_gate=SimpleNamespace(flush=lambda: ""), put=put,
        )
        await _handle_dm_verdict_event(run, {"status": "active", "should_continue": False, "turns": 1})
        with Session(engine) as s:
            merged = s.get(Conversation, cid)
            assert merged.deathmatch_plan_version == 2, "测试必须先证明 own merge 已真实提交 v2"
        step = fresh.deathmatch_plan["steps"][0]
        mgr._set_step_status(step, "done", "after_own_merge")
        mgr._record_settled({"type": "step_complete", "step": step})
        await wt.drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_plan["steps"][0]["status"] == "done", (
            "own merge 后 CAS 基线必须随已提交合同推进，后续 transition 不得永久 rejected"
        )
        assert row.deathmatch_settled_ledger


def test_repeated_stale_merge_cannot_reopen_pending_contract(monkeypatch):
    from app.db.database import Conversation
    import app.api.chat_stream_transport as transport
    from app.services.deathmatch_service import DEATHMATCH_SYNC_FIELDS

    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    transport._sync_chat_globals()
    with Session(engine) as s:
        fresh = s.get(Conversation, cid)
        fresh.deathmatch_last_verification_result = {"verdict": "partial", "v": 1}
        fresh.deathmatch_reflections = ["old-reflection"]
        fresh.deathmatch_events = ["old-event"]
        s.refresh(fresh)
        snapshot = {f: copy.deepcopy(getattr(fresh, f)) for f in DEATHMATCH_SYNC_FIELDS}
        s.expunge(fresh)
    mgr = DeathmatchManager(SimpleNamespace(id="other", deathmatch_plan=None))
    transport._rebind_deathmatch_manager_conversation(mgr, fresh)

    async def go():
        async def put(_event):
            return None
        run = SimpleNamespace(
            conversation_id=cid, task_conversation=fresh,
            dm_sync_snapshot=snapshot, dm_sync_frozen=frozenset(),
            dm_final_attachments=[], assistant_content="", content_segments=[],
            display_sequence=[], tool_results_accumulated=[], search_queries_used=[],
            search_queries_by_call={}, tool_call_events_accumulated=[],
            pre_tool_gate=SimpleNamespace(flush=lambda: ""), put=put,
        )
        step = fresh.deathmatch_plan["steps"][0]
        mgr._set_step_status(step, "in_progress", "verify_pick")
        await transport._handle_dm_verdict_event(
            run, {"status": "active", "should_continue": False, "turns": 1},
        )
        with Session(engine) as s:
            final = s.get(Conversation, cid)
            final.deathmatch_plan = {"steps": [{"id": "s1", "status": "done"}], "v": 3}
            final.deathmatch_plan_version = 3
            final.deathmatch_status = "done"
            final.deathmatch_last_verification_result = {"verdict": "done", "v": 3}
            final.deathmatch_reflections = ["new-reflection"]
            final.deathmatch_events = ["new-event"]
            s.commit()
        # Repeated old merges must not learn the new snapshot and then reopen it.
        await transport._handle_dm_verdict_event(
            run, {"status": "active", "should_continue": False, "turns": 2},
        )
        await transport._handle_dm_verdict_event(
            run, {"status": "active", "should_continue": False, "turns": 3},
        )

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_plan["steps"][0]["status"] == "done", (
            "DB 已 done 后，重复旧 merge 不得把 pending/in_progress 写回"
        )
        assert row.deathmatch_plan_version == 3
        assert row.deathmatch_last_verification_result == {"verdict": "done", "v": 3}
        assert row.deathmatch_reflections == ["new-reflection"]
        assert "new-event" in row.deathmatch_events
        assert "old-event" not in row.deathmatch_events


def test_external_contract_conflict_merge_does_not_authorize_stale_source(monkeypatch):
    from app.db.database import Conversation
    import app.api.chat_stream_transport as transport
    from app.services.deathmatch_service import DEATHMATCH_SYNC_FIELDS
    import app.services.deathmatch_write_through as wt

    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    transport._sync_chat_globals()
    with Session(engine) as s:
        fresh = s.get(Conversation, cid)
        fresh.deathmatch_goal = "G1"
        fresh.deathmatch_plan = {"steps": [{"id": "old", "status": "pending"}], "v": 1}
        fresh.deathmatch_plan_version = 1
        fresh.deathmatch_acceptance_criteria = [{"id": "old", "text": "旧合同"}]
        fresh.deathmatch_grilling_qa_history = []
        s.commit()
        s.refresh(fresh)
        snapshot = {f: copy.deepcopy(getattr(fresh, f)) for f in DEATHMATCH_SYNC_FIELDS}
        s.expunge(fresh)
    mgr = DeathmatchManager(SimpleNamespace(id="other", deathmatch_plan=None))
    transport._rebind_deathmatch_manager_conversation(mgr, fresh)
    with Session(engine) as s:
        external = s.get(Conversation, cid)
        external.deathmatch_goal = "G2"
        external.deathmatch_plan = None
        external.deathmatch_plan_version = 2
        external.deathmatch_acceptance_criteria = None
        external.deathmatch_grilling_qa_history = [{"goal_contract_pending": True}]
        s.commit()

    async def go():
        async def put(_event):
            return None
        run = SimpleNamespace(
            conversation_id=cid, task_conversation=fresh,
            dm_sync_snapshot=snapshot, dm_sync_frozen=frozenset(),
            dm_final_attachments=[], assistant_content="", content_segments=[],
            display_sequence=[], tool_results_accumulated=[], search_queries_used=[],
            search_queries_by_call={}, tool_call_events_accumulated=[],
            pre_tool_gate=SimpleNamespace(flush=lambda: ""), put=put,
        )
        await transport._handle_dm_verdict_event(
            run, {"status": "active", "should_continue": False, "turns": 1},
        )
        wt.write_through_bg(
            fresh,
            deathmatch_plan=fresh.deathmatch_plan,
            deathmatch_plan_version=fresh.deathmatch_plan_version,
            deathmatch_acceptance_criteria=fresh.deathmatch_acceptance_criteria,
            deathmatch_grilling_qa_history=fresh.deathmatch_grilling_qa_history,
        )
        await wt.drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_goal == "G2"
        assert row.deathmatch_plan is None, "外部冲突 merge 不得授权旧 source 复活 P1"
        assert row.deathmatch_acceptance_criteria is None
        assert row.deathmatch_grilling_qa_history[-1]["goal_contract_pending"] is True


def test_rebind_lazy_contract_commit_then_step_write_through(monkeypatch):
    from app.db.database import Conversation
    import app.api.chat_stream_transport as transport
    from app.services.deathmatch_service import DeathmatchManager
    import app.services.deathmatch_write_through as wt

    engine, cid = _env()
    _patch_factory(monkeypatch, engine)

    async def go():
        async with transport.AsyncSessionLocal() as db:
            fresh = await db.get(Conversation, cid)
            mgr = DeathmatchManager(fresh)
            transport._rebind_deathmatch_manager_conversation(mgr, fresh)
            fresh.deathmatch_plan = {"steps": [{"id": "s1", "status": "in_progress"}], "v": 2}
            fresh.deathmatch_plan_version = 2
            commit_contract = getattr(transport, "_commit_deathmatch_contract", None)
            assert commit_contract is not None, "lazy contract commit 必须提供 CAS 基线同步 seam"
            await commit_contract(db, fresh)
            step = fresh.deathmatch_plan["steps"][0]
            mgr._set_step_status(step, "done", "after_lazy_contract_commit")
            mgr._record_settled({"type": "step_complete", "step": step})
            await wt.drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_plan["steps"][0]["status"] == "done"
        assert row.deathmatch_settled_ledger


def test_verdict_merge_drains_own_write_through_before_cas(monkeypatch):
    """R16 close-out：own commit 与 baseline 推进之间不得被误判为外部 G2。

    在 DB commit 已成功、FIFO 链节尚未推进 detached baseline 的合法窗口内
    进入 done verdict merge。merge 必须先排空同会话写-through，否则旧 runner
    会被标 stale，后续 finalize_close_out 的 done 步骤写也永久被拒绝。
    """
    from app.db.database import Conversation
    import app.api.chat_stream_transport as transport
    from app.services.deathmatch_service import DEATHMATCH_SYNC_FIELDS
    import app.services.deathmatch_write_through as wt

    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    transport._sync_chat_globals()
    with Session(engine) as s:
        fresh = s.get(Conversation, cid)
        s.refresh(fresh)
        snapshot = {f: copy.deepcopy(getattr(fresh, f)) for f in DEATHMATCH_SYNC_FIELDS}
        s.expunge(fresh)
    mgr = DeathmatchManager(fresh)
    transport._rebind_deathmatch_manager_conversation(mgr, fresh)

    original_write = wt._write_columns
    first_commit = None
    release_first = None

    async def go():
        nonlocal first_commit, release_first
        first_commit = asyncio.Event()
        release_first = asyncio.Event()

        async def slow_first_write(conv_id, columns, **kwargs):
            written = await original_write(conv_id, columns, **kwargs)
            plan = columns.get("deathmatch_plan") or {}
            steps = plan.get("steps") or []
            if (
                written and steps
                and steps[0].get("status") == "in_progress"
                and not first_commit.is_set()
            ):
                # Model the real post-commit/pre-baseline async window.
                first_commit.set()
                await release_first.wait()
            return written

        monkeypatch.setattr(wt, "_write_columns", slow_first_write)
        step = fresh.deathmatch_plan["steps"][0]
        mgr._set_step_status(step, "in_progress", "verify_pick")
        await first_commit.wait()
        assert fresh._dm_write_contract_baseline["deathmatch_plan_version"] == 1

        # finalize_close_out mutates the same detached row and queues done after
        # the still-unreturned in_progress chain link.
        fresh.deathmatch_status = "done"
        fresh.deathmatch_verdict = "done"
        fresh.deathmatch_reason = "机械验收通过"
        fresh.deathmatch_last_verification_result = {"status": "complete"}
        mgr._set_step_status(step, "done", "finalize_close_out")

        async def put(_event):
            release_first.set()

        run = SimpleNamespace(
            conversation_id=cid, task_conversation=fresh,
            dm_sync_snapshot=snapshot, dm_sync_frozen=frozenset(),
            dm_final_attachments=[], assistant_content="", content_segments=[],
            display_sequence=[], tool_results_accumulated=[], search_queries_used=[],
            search_queries_by_call={}, tool_call_events_accumulated=[],
            pre_tool_gate=SimpleNamespace(flush=lambda: ""), put=put,
        )
        await transport._handle_dm_verdict_event(run, {
            "status": "done", "should_continue": False, "turns": 0,
        })
        await wt.drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_status == "done"
        steps = (row.deathmatch_plan or {}).get("steps") or []
        assert steps, "终态回归不得用空计划制造 vacuous pass"
        assert all(
            step.get("status") == "done"
            for step in steps
        ), "own commit/baseline 间隙不得让 done verdict 永久留下非 done steps"
        assert row.deathmatch_verdict == "done"
        assert row.deathmatch_last_verification_result == {"status": "complete"}


def test_lazy_consumed_goal_epoch_survives_next_step_write(monkeypatch):
    """生产形状：pending=true 经 lazy commit 消费后，下一状态写不得恢复 true。"""
    from app.db.database import Conversation
    import app.api.chat_stream_transport as transport
    from app.services.deathmatch_service import DEATHMATCH_SYNC_FIELDS
    import app.services.deathmatch_write_through as wt

    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    transport._sync_chat_globals()
    with Session(engine) as s:
        seeded = s.get(Conversation, cid)
        seeded.deathmatch_grilling_qa_history = []
        s.commit()

    async def go():
        async with transport.AsyncSessionLocal() as db:
            conv = await db.get(Conversation, cid)
            mgr = DeathmatchManager(conv)
            transport._rebind_deathmatch_manager_conversation(mgr, conv)
            snapshot = {
                field: copy.deepcopy(getattr(conv, field))
                for field in DEATHMATCH_SYNC_FIELDS
            }
            conv.deathmatch_grilling_qa_history = [
                {"round": 1, "qa_pairs": [{"question": "目录", "answer": "当前目录"}]},
                {"round": 2, "readiness": {"should_continue": False}},
                {"goal_contract_pending": True, "plan_version": 1},
            ]
            conv.deathmatch_plan = {"steps": [{"id": "s1", "status": "pending"}], "v": 2}
            conv.deathmatch_plan_version = 2
            mgr._consume_fresh_goal_obligations()
            await transport._commit_deathmatch_contract(db, conv)
            assert conv._dm_write_contract_baseline[
                "deathmatch_grilling_qa_history"
            ][-1]["goal_contract_pending"] is False
        with Session(engine) as s:
            committed_row = s.get(Conversation, cid)
            assert len(committed_row.deathmatch_grilling_qa_history) == 3, (
                "lazy commit 不得把既有 QA/readiness 历史清空"
            )
            assert committed_row.deathmatch_grilling_qa_history[0]["qa_pairs"]
            assert committed_row.deathmatch_grilling_qa_history[1]["readiness"]
            assert committed_row.deathmatch_grilling_qa_history[-1][
                "goal_contract_pending"
            ] is False, "lazy commit 本身必须先真实持久化 pending=false"
        mgr._set_step_status(conv.deathmatch_plan["steps"][0], "in_progress", "verify_pick")
        await wt.drain_write_through()
        conv.deathmatch_status = "done"
        conv.deathmatch_verdict = "done"
        conv.deathmatch_reason = "合法 own-commit close-out"
        conv.deathmatch_last_verification_result = {"status": "complete"}
        mgr._set_step_status(conv.deathmatch_plan["steps"][0], "done", "finalize_close_out")

        async def put(_event):
            return None
        run = SimpleNamespace(
            conversation_id=cid, task_conversation=conv,
            dm_sync_snapshot=snapshot, dm_sync_frozen=frozenset(),
            dm_final_attachments=[], assistant_content="", content_segments=[],
            display_sequence=[], tool_results_accumulated=[], search_queries_used=[],
            search_queries_by_call={}, tool_call_events_accumulated=[],
            pre_tool_gate=SimpleNamespace(flush=lambda: ""), put=put,
        )
        await transport._handle_dm_verdict_event(run, {
            "status": "done", "should_continue": False, "turns": 0,
        })
        await wt.drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_grilling_qa_history[-1]["goal_contract_pending"] is False, (
            "lazy consume 后的普通 step write 不得把旧 pending=true 回灌到 DB"
        )
        steps = (row.deathmatch_plan or {}).get("steps") or []
        assert row.deathmatch_status == "done"
        assert steps and all(step.get("status") == "done" for step in steps)
        assert row.deathmatch_verdict == "done"
        assert row.deathmatch_last_verification_result == {"status": "complete"}


def test_external_g2_rejects_stale_terminal_control_state(monkeypatch):
    """外部 G2 合同已替换后，旧 G1 runner 不得再提交 done 终态。"""
    from app.db.database import Conversation
    import app.api.chat_stream_transport as transport
    from app.services.deathmatch_service import DEATHMATCH_SYNC_FIELDS

    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    transport._sync_chat_globals()
    with Session(engine) as s:
        fresh = s.get(Conversation, cid)
        snapshot = {f: copy.deepcopy(getattr(fresh, f)) for f in DEATHMATCH_SYNC_FIELDS}
        s.expunge(fresh)
    mgr = DeathmatchManager(fresh)
    transport._rebind_deathmatch_manager_conversation(mgr, fresh)
    with Session(engine) as s:
        external = s.get(Conversation, cid)
        external.deathmatch_goal = "G2"
        external.deathmatch_status = "active"
        external.deathmatch_reason = "G2 active"
        external.deathmatch_verdict = "continue"
        external.deathmatch_plan = {"steps": [{"id": "g2", "status": "pending"}], "v": 2}
        external.deathmatch_plan_version = 2
        external.deathmatch_acceptance_criteria = [{"id": "g2"}]
        external.deathmatch_grilling_qa_history = [{"goal_contract_pending": True}]
        external.deathmatch_last_verification_result = {"status": "partial"}
        s.commit()

    async def go():
        fresh.deathmatch_status = "done"
        fresh.deathmatch_reason = "stale G1 done"
        fresh.deathmatch_verdict = "done"
        fresh.deathmatch_last_verification_result = {"status": "complete"}
        async def put(_event):
            return None
        run = SimpleNamespace(
            conversation_id=cid, task_conversation=fresh,
            dm_sync_snapshot=snapshot, dm_sync_frozen=frozenset(),
            dm_final_attachments=[], assistant_content="", content_segments=[],
            display_sequence=[], tool_results_accumulated=[], search_queries_used=[],
            search_queries_by_call={}, tool_call_events_accumulated=[],
            pre_tool_gate=SimpleNamespace(flush=lambda: ""), put=put,
        )
        await transport._handle_dm_verdict_event(run, {
            "status": "done", "should_continue": False, "turns": 0,
        })

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_goal == "G2"
        assert row.deathmatch_plan["steps"] == [{"id": "g2", "status": "pending"}]
        assert row.deathmatch_status == "active", "stale G1 不得把替代合同 G2 标为 done"
        assert row.deathmatch_reason == "G2 active"
        assert row.deathmatch_verdict == "continue"
        assert row.deathmatch_last_verification_result == {"status": "partial"}


def test_verdict_rebase_row_lock_blocks_external_g2_until_commit(monkeypatch):
    """freshness read 后外部 G2 必须等行锁释放，不能被旧 merge 回盖。"""
    from app.db.database import Conversation
    import app.api.chat_stream_transport as transport
    from app.services.deathmatch_service import DEATHMATCH_SYNC_FIELDS
    import app.services.deathmatch_write_through as wt

    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    transport._sync_chat_globals()
    with Session(engine) as s:
        fresh = s.get(Conversation, cid)
        snapshot = {f: copy.deepcopy(getattr(fresh, f)) for f in DEATHMATCH_SYNC_FIELDS}
        s.expunge(fresh)
    mgr = DeathmatchManager(fresh)
    transport._rebind_deathmatch_manager_conversation(mgr, fresh)

    class _LockingShim(_AsyncShim):
        def __init__(self, session, lock, *, yield_on_commit):
            super().__init__(session)
            self._lock = lock
            self._yield_on_commit = yield_on_commit
            self._lock_held = False

        async def execute(self, stmt):
            if getattr(stmt, "_for_update_arg", None) is not None and not self._lock_held:
                await self._lock.acquire()
                self._lock_held = True
            return await super().execute(stmt)

        async def commit(self):
            if self._yield_on_commit:
                await asyncio.sleep(0)
            await super().commit()
            if self._lock_held:
                self._lock_held = False
                self._lock.release()

        async def __aexit__(self, *exc):
            if self._lock_held:
                self._lock_held = False
                self._lock.release()
            return await super().__aexit__(*exc)

    async def go():
        import app.api.chat as chat
        import app.db.database as dbm
        lock = asyncio.Lock()

        def handler_factory():
            return _LockingShim(Session(engine), lock, yield_on_commit=True)

        def external_factory():
            return _LockingShim(Session(engine), lock, yield_on_commit=False)

        monkeypatch.setattr(dbm, "AsyncSessionLocal", handler_factory)
        monkeypatch.setattr(chat, "AsyncSessionLocal", handler_factory)
        monkeypatch.setattr(transport, "AsyncSessionLocal", handler_factory)

        async def external_write():
            async with external_factory() as db:
                # Model the database wait performed by any external UPDATE.
                await lock.acquire()
                try:
                    row = await db.get(Conversation, cid)
                    row.deathmatch_goal = "G2"
                    row.deathmatch_plan = {"steps": [{"id": "g2", "status": "pending"}], "v": 2}
                    row.deathmatch_plan_version = 2
                    row.deathmatch_acceptance_criteria = [{"id": "g2"}]
                    row.deathmatch_grilling_qa_history = [{"goal_contract_pending": True}]
                    await db.commit()
                finally:
                    lock.release()

        external_task = None
        original_matches = wt._snapshot_matches

        def pause_after_freshness_read(row, expected):
            nonlocal external_task
            matched = original_matches(row, expected)
            if matched and external_task is None:
                external_task = asyncio.create_task(external_write())
            return matched

        monkeypatch.setattr(wt, "_snapshot_matches", pause_after_freshness_read)
        fresh.deathmatch_plan = {"steps": [{"id": "g1", "status": "in_progress"}], "v": 2}
        fresh.deathmatch_plan_version = 2
        fresh.deathmatch_acceptance_criteria = [{"id": "g1"}]
        fresh.deathmatch_grilling_qa_history = []

        async def put(_event):
            return None
        run = SimpleNamespace(
            conversation_id=cid, task_conversation=fresh,
            dm_sync_snapshot=snapshot, dm_sync_frozen=frozenset(),
            dm_final_attachments=[], assistant_content="", content_segments=[],
            display_sequence=[], tool_results_accumulated=[], search_queries_used=[],
            search_queries_by_call={}, tool_call_events_accumulated=[],
            pre_tool_gate=SimpleNamespace(flush=lambda: ""), put=put,
        )
        await transport._handle_dm_verdict_event(run, {
            "status": "active", "should_continue": False, "turns": 1,
        })
        assert external_task is not None
        await external_task

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_goal == "G2"
        assert row.deathmatch_plan["steps"] == [{"id": "g2", "status": "pending"}], (
            "外部 G2 在 freshness read 后提交时，旧 merge 不得在无行锁情况下回盖 G1"
        )
        assert row.deathmatch_acceptance_criteria == [{"id": "g2"}]
        assert row.deathmatch_grilling_qa_history[-1]["goal_contract_pending"] is True


def test_cancelling_drain_waiter_does_not_cancel_shared_fifo(monkeypatch):
    """取消 verdict/停止侧等待者不得取消共享 close-out FIFO。"""
    from app.db.database import Conversation
    import app.api.chat_stream_transport as transport
    import app.services.deathmatch_write_through as wt

    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    with Session(engine) as s:
        fresh = s.get(Conversation, cid)
        s.expunge(fresh)
    mgr = DeathmatchManager(fresh)
    transport._rebind_deathmatch_manager_conversation(mgr, fresh)
    original_write = wt._write_columns

    async def go():
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def gated_first_write(conv_id, columns, **kwargs):
            plan = columns.get("deathmatch_plan") or {}
            steps = plan.get("steps") or []
            if steps and steps[0].get("status") == "in_progress" and not first_started.is_set():
                first_started.set()
                await release_first.wait()
            return await original_write(conv_id, columns, **kwargs)

        monkeypatch.setattr(wt, "_write_columns", gated_first_write)
        step = fresh.deathmatch_plan["steps"][0]
        mgr._set_step_status(step, "in_progress", "verify_pick")
        await first_started.wait()
        fresh.deathmatch_status = "done"
        mgr._set_step_status(step, "done", "finalize_close_out")

        waiter = asyncio.create_task(wt.drain_write_through(cid))
        await asyncio.sleep(0)
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("测试必须先证明 drain waiter 已被取消")
        release_first.set()
        await wt.drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        steps = (row.deathmatch_plan or {}).get("steps") or []
        assert steps and all(step.get("status") == "done" for step in steps), (
            "取消 drain waiter 不得沿 await 链取消共享 FIFO 或后续 close-out 写"
        )


# ── A-N1：基线快照成员口径（保 mtime 产物不误拒） ────────────────────────

def _conv_with_plan(**over):
    import time as _t
    plan = {"steps": [{"id": "s1", "status": "in_progress", "description": "d",
                       "expected_output": "deliverable.md",
                       "output_files": ["deliverable.md"]}],
            "created_at": _t.time() - 3600}
    plan.update(over.pop("plan_over", {}))
    base = dict(
        id="conv-an1", deathmatch_mode=True, deathmatch_status="active",
        deathmatch_goal="测试目标", deathmatch_turns=3, deathmatch_max_turns=0,
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


def _mgr_llm(conv, parsed=None):
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


def test_lane1_accepts_in_run_artifact_with_preserved_mtime(tmp_path):
    """A-N1（D-17）：unzip/tar 保 mtime 的目标内产出（不在基线快照）不得误拒。"""
    import os as _os
    import time as _t
    ws = str(tmp_path)
    fp = tmp_path / "deliverable.md"
    fp.write_text("L" * 200, encoding="utf-8")
    old = _t.time() - 86400
    _os.utime(fp, (old, old))  # 保留历史 mtime（归档解压形态）
    conv = _conv_with_plan(plan_over={"baseline_paths": ["outline.txt"]})
    conv.deathmatch_last_verification_result = {
        "status": "partial", "workspace_files": [
            {"path": "deliverable.md", "size": 200, "ext": ".md", "mtime": int(old)}],
        "completed_steps": []}
    mgr = _mgr_llm(conv)
    _run(mgr.verify_step_outputs("已完成", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] == "done", "目标内产出（非基线成员）应记账放行"


def test_lane1_rejects_baseline_member_with_old_mtime(tmp_path):
    """A-N1：基线快照成员（目标启动前遗留）仍拒——phantom 面不回退。"""
    import os as _os
    import time as _t
    ws = str(tmp_path)
    fp = tmp_path / "deliverable.md"
    fp.write_text("L" * 200, encoding="utf-8")
    old = _t.time() - 86400
    _os.utime(fp, (old, old))
    conv = _conv_with_plan(plan_over={"baseline_paths": ["deliverable.md"]})
    conv.deathmatch_last_verification_result = {
        "status": "partial", "workspace_files": [
            {"path": "deliverable.md", "size": 200, "ext": ".md", "mtime": int(old)}],
        "completed_steps": []}
    mgr = _mgr_llm(conv)
    _run(mgr.verify_step_outputs("已完成", ws))
    step = conv.deathmatch_plan["steps"][0]
    assert step["status"] != "done", "基线成员（遗留）不得记账放行"


def test_baseline_round_records_baseline_paths(tmp_path):
    """A-N1：首轮基线轮把工作区清单钉进 plan.baseline_paths（出处口径）。"""
    ws = str(tmp_path)
    (tmp_path / "legacy.txt").write_text("x" * 10, encoding="utf-8")
    conv = _conv_with_plan()
    conv.deathmatch_last_verification_result = None  # 基线轮
    mgr = _mgr_llm(conv)
    _run(mgr.verify_step_outputs("第一轮", ws))
    assert "legacy.txt" in (conv.deathmatch_plan.get("baseline_paths") or []), (
        "基线轮必须记录 baseline_paths")


# ── A-N2：local_patch 替换迁移事件（pending→pending 亦记） ───────────────

def test_local_patch_records_transition_on_pending_step():
    import json as _json
    conv = _conv_with_plan()
    conv.deathmatch_plan["steps"][0]["status"] = "pending"
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return _json.dumps({"steps": [{
            "id": "s1", "description": "重写", "expected_output": "a.md"}]},
            ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    mgr._judge_steps_needing_evidence_spec = lambda steps, **k: asyncio.sleep(0, result=None)
    ok = _run(mgr._local_patch_step(conv.deathmatch_plan["steps"][0], ["问题"]))
    assert ok
    transitions = [e for e in (conv.deathmatch_events or []) if e.get("type") == "step_transition"]
    assert transitions and transitions[-1].get("cause") == "local_patch", (
        "A-N2：pending→pending 替换也必须留 step_transition 事件")


# ── I2/A-N2：seforce_event 缝 + 写点全覆盖 ─────────────────────────────────

def test_set_step_status_force_event_same_state_records_event():
    """I2：force_event=True 时同态（pending→pending）也记迁移事件。"""
    conv = _conv_with_plan()
    mgr = DeathmatchManager(conv)
    step = conv.deathmatch_plan["steps"][0]
    step["status"] = "pending"
    mgr._set_step_status(step, "pending", "local_patch", force_event=True)
    transitions = [e for e in (conv.deathmatch_events or []) if e.get("type") == "step_transition"]
    assert transitions and transitions[-1].get("from") == "pending" \
        and transitions[-1].get("to") == "pending" \
        and transitions[-1].get("cause") == "local_patch", \
        "force_event：同态也必须记迁移事件"


def test_local_patch_plan_reassignment_write_through(monkeypatch):
    """I2：local_patch 计划重赋值后必须 write-through 落库（detached、无 merge）。"""
    import json as _json
    from app.db.database import Conversation
    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        conv.deathmatch_plan = {"steps": [{"id": "s1", "status": "in_progress",
                                           "description": "旧"}]}
        s.commit()
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        s.expunge(conv)
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return _json.dumps({"steps": [{
            "id": "s1", "description": "重写后的步骤", "expected_output": "a.md"}]},
            ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen

    async def fake_judge(steps, **k):
        return None

    mgr._judge_steps_needing_evidence_spec = fake_judge

    async def go():
        ok = await mgr._local_patch_step(conv.deathmatch_plan["steps"][0], ["问题"])
        assert ok
        await drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_plan["steps"][0]["description"] == "重写后的步骤", \
            "local_patch 计划重赋值必须 write-through 落库（无 merge 通道）"
        assert any(e.get("cause") == "local_patch"
                   for e in (row.deathmatch_events or [])
                   if e.get("type") == "step_transition"), "替换迁移事件必须落库"


def test_gate_blocks_increment_write_through(monkeypatch):
    """I2：A1a gate_blocks 自增处补 write-through（同态 _set_step_status 早退
    不带写-through——自增不得只靠 merge）。"""
    from app.db.database import Conversation
    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        conv.deathmatch_plan = {"steps": [{"id": "s1", "status": "in_progress",
                                           "description": "合成步"}]}  # 无声明产出
        s.commit()
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        s.expunge(conv)
    mgr = _mgr_llm(conv)
    conv.deathmatch_last_verification_result = {
        "status": "partial", "workspace_files": [], "completed_steps": []}

    async def go():
        await mgr.verify_step_outputs("已完成", "")
        await drain_write_through()

    _run(go())
    assert conv.deathmatch_plan["steps"][0].get("gate_blocks") == 1
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert int((row.deathmatch_plan["steps"][0].get("gate_blocks") or 0)) >= 1, \
            "gate_blocks 自增必须 write-through 落库（无 merge）"


# ── I1：写-through 串行化 + 执行期取值 + 白名单 ────────────────────────────

def test_write_through_rejects_control_and_unknown_columns():
    """I1 Security Minor：白名单外列 raise ValueError（控制字段永不写）。"""
    from app.services.deathmatch_write_through import write_through_bg
    conv = SimpleNamespace(id="conv-x", deathmatch_plan=None)
    with pytest.raises(ValueError):
        write_through_bg(conv, deathmatch_status="active")
    with pytest.raises(ValueError):
        write_through_bg(conv, deathmatch_pause_state={})
    # 冻结白名单 8 列放行（非 ORM 对象静默跳过，不抛）
    write_through_bg(conv, deathmatch_plan=None, deathmatch_plan_version=1,
                     deathmatch_events=[], deathmatch_settled_ledger=[],
                     deathmatch_reflections=[], deathmatch_evidence_units=[])


def test_write_through_exec_time_values_converge_to_merged(monkeypatch):
    """I1 判别：写-through 排队时值=P1、merge 面对 P2 → 终态 P2 落库。
    执行期取值（任务闭包持 conv 引用，执行时读列值）——旧快照回灌=回归。"""
    from app.db.database import Conversation
    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        s.expunge(conv)
    p1 = {"steps": [{"id": "s1", "status": "pending"}], "v": 1}
    p2 = {"steps": [{"id": "s1", "status": "done"}], "v": 2}

    async def go():
        conv.deathmatch_plan = p1
        from app.services.deathmatch_write_through import write_through_bg
        write_through_bg(conv, deathmatch_plan=p1)
        # merge 面对 P2：运行面推进 + merge 通道提交 P2
        conv.deathmatch_plan = p2
        with Session(engine) as s:
            row = s.get(Conversation, cid)
            row.deathmatch_plan = p2
            s.commit()
        await drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_plan == p2, \
            "写-through 执行期取值——不得用排队时旧快照 P1 回灌 merge 后的 P2"


def test_write_through_per_conv_fifo_serialized(monkeypatch):
    """I1：同 conv 写-through FIFO 串行（后写不得乱序抢先）。"""
    import itertools
    import app.services.deathmatch_write_through as wt
    from app.db.database import Conversation
    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    exits: list = []
    counter = itertools.count()

    async def fake_write(conv_id, columns, **_kwargs):
        n = next(counter)
        if n == 0:
            await asyncio.sleep(0.05)  # 首写慢——无串行则后写乱序抢先
        exits.append(n)

    monkeypatch.setattr(wt, "_write_columns", fake_write)
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        s.expunge(conv)

    async def go():
        conv.deathmatch_plan = {"a": 1}
        wt.write_through_bg(conv, deathmatch_plan=conv.deathmatch_plan)
        conv.deathmatch_plan = {"a": 2}
        wt.write_through_bg(conv, deathmatch_plan=conv.deathmatch_plan)
        await drain_write_through()

    _run(go())
    assert exits == [0, 1], f"同 conv 写-through 必须 FIFO 串行: {exits}"


def test_write_through_config_switch(monkeypatch):
    """Minors：deathmatch_write_through_enabled 属性（默认 True）+ 开关关=不落库。"""
    from app.core.config import get_config
    from app.db.database import Conversation
    cfg = get_config()
    assert cfg.deathmatch_write_through_enabled is True, "默认 True"
    monkeypatch.setitem(cfg._config["deathmatch"], "write_through_enabled", False)
    assert cfg.deathmatch_write_through_enabled is False
    engine, cid = _env()
    _patch_factory(monkeypatch, engine)
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        s.expunge(conv)
    mgr = DeathmatchManager(conv)

    async def go():
        mgr._set_step_status(conv.deathmatch_plan["steps"][0], "done", "unit")
        await drain_write_through()

    _run(go())
    with Session(engine) as s:
        row = s.get(Conversation, cid)
        assert row.deathmatch_plan["steps"][0]["status"] == "pending", \
            "开关关时写-through 不得落库"
