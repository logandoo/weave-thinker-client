"""TDD — R3 持久化治本（诊断 2026-10-13 机制 B）+ R5 步骤迁移事件。

判别测试 pin 三组 ORM 语义（SQLAlchemy 2.x JSON 列）：
  A) 无触点原地变更 = 不落库（旧语义陷阱，防止未来"以为落库了"）
  B) 原地变更 + _touch_json 触点 = 落库（R3 新语义）
  C) 浅拷贝重赋值 dict(plan) = no-op（诊断 PoC B：dirty=1 但 DB 不更新）
  D) _set_step_status = 写状态 + 迁移事件 + 触脏（R5/R3 单接缝）
  E) 非 ORM 伪会话（SimpleNamespace）触点 no-op 不炸

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_deathmatch_json_persistence.py -v
"""
import os
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.deathmatch_service as ds
from app.services.deathmatch_service import DeathmatchManager


@compiles(JSONB, "sqlite")
def _r3_jsonb_as_json(element, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _r3_bigint_as_int(element, compiler, **kw):
    return "INTEGER"


def _env():
    """SQLite in-memory + 真 ORM Conversation 行（仿 test_stream_persistence 先例）。"""
    from app.db.database import Base, Conversation, User

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__, Conversation.__table__])
    with Session(engine) as s:
        u = User(username="r3", password_hash="x")
        s.add(u)
        s.flush()
        c = Conversation(
            user_id=u.id,
            title="t",
            deathmatch_plan={"steps": [
                {"id": "s1", "status": "pending", "description": "步骤1"},
            ]},
            deathmatch_plan_version=1,
        )
        s.add(c)
        s.commit()
        return engine, c.id


# ── A) 旧语义 pin：无触点原地变更不落库 ─────────────────────────────────
def test_inplace_mutation_without_touch_not_persisted():
    from app.db.database import Conversation
    engine, cid = _env()
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        conv.deathmatch_plan["steps"][0]["status"] = "done"
        s.commit()
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        assert conv.deathmatch_plan["steps"][0]["status"] == "pending", (
            "无触点原地变更竟然落库了——旧语义 pin 失效，需人工复核 ORM 行为面"
        )


# ── B) R3 新语义：触点后落库 ─────────────────────────────────────────────
def test_inplace_mutation_with_touch_persists():
    from app.db.database import Conversation
    engine, cid = _env()
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        conv.deathmatch_plan["steps"][0]["status"] = "done"
        ds._touch_json(conv, "deathmatch_plan")
        s.commit()
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        assert conv.deathmatch_plan["steps"][0]["status"] == "done"


# ── C) 浅拷贝重赋值 no-op 陷阱 pin（诊断 PoC B）──────────────────────────
def test_shallow_copy_reassign_is_noop_trap():
    from app.db.database import Conversation
    engine, cid = _env()
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        plan = dict(conv.deathmatch_plan)  # 浅拷贝：内层 steps 列表同引用
        plan["steps"][0]["status"] = "done"
        conv.deathmatch_plan = plan
        s.commit()
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        assert conv.deathmatch_plan["steps"][0]["status"] == "pending", (
            "浅拷贝重赋值竟然落库了——陷阱 pin 失效（或 SQLAlchemy 行为已变）"
        )


# ── D) _set_step_status：状态 + 迁移事件 + 触脏 ─────────────────────────
def test_set_step_status_marks_dirty_and_records_transition():
    from app.db.database import Conversation
    engine, cid = _env()
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        mgr = DeathmatchManager(conv)
        step = conv.deathmatch_plan["steps"][0]
        mgr._set_step_status(step, "done", "unit_test_cause")
        s.commit()
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        assert conv.deathmatch_plan["steps"][0]["status"] == "done"
        events = conv.deathmatch_events or []
        transitions = [e for e in events if e.get("type") == "step_transition"]
        assert transitions, "缺少 step_transition 迁移事件（R5）"
        ev = transitions[-1]
        assert ev.get("step_id") == "s1"
        assert ev.get("from") == "pending"
        assert ev.get("to") == "done"
        assert ev.get("cause") == "unit_test_cause"


def test_set_step_status_same_status_records_no_transition():
    from app.db.database import Conversation
    engine, cid = _env()
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        mgr = DeathmatchManager(conv)
        step = conv.deathmatch_plan["steps"][0]
        mgr._set_step_status(step, "pending", "noop_cause")
        s.commit()
    with Session(engine) as s:
        conv = s.get(Conversation, cid)
        events = [e for e in (conv.deathmatch_events or []) if e.get("type") == "step_transition"]
        assert not events


# ── E) 非 ORM 伪会话 no-op ──────────────────────────────────────────────
def test_touch_json_noop_on_non_orm_object():
    fake = SimpleNamespace(deathmatch_plan={"steps": []})
    ds._touch_json(fake, "deathmatch_plan")  # 不得抛异常
    fake2 = SimpleNamespace(deathmatch_plan=None)
    ds._touch_json(fake2, "deathmatch_plan")


def test_set_step_status_works_on_simple_namespace_conv():
    conv = SimpleNamespace(
        id="conv-x",
        deathmatch_turns=1,
        deathmatch_events=[],
        deathmatch_plan={"steps": [{"id": "s1", "status": "pending"}]},
    )
    mgr = DeathmatchManager(conv)
    mgr._set_step_status(conv.deathmatch_plan["steps"][0], "in_progress", "pick")
    assert conv.deathmatch_plan["steps"][0]["status"] == "in_progress"
    assert any(e.get("type") == "step_transition" for e in conv.deathmatch_events)
