"""TDD — W1 M3b① 死磕反思撤回（论文 2610.12668 ReHarness 线；勘正 A5）
（docs/AUDIT_2026-10-13_deathmatch_memory_papers.md §3.3/§10）。

A5 勘正：反思条目补 id/retracted 字段（JSON 演化，旧条目按缺省兼容）；
同签名 ×3 升级发生时标 retracted + 写修正指令，渲染为「避免」块；
撤回后升级窗口只统计未撤回条目（否则旧签名持续触发）。
开关 deathmatch_reflection_retract_enabled 默认关。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_deathmatch_reflection_retract.py -v
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.deathmatch_reflection as refl
from app.services.deathmatch_reflection import ReflectionMemory


def _conv():
    return SimpleNamespace(id="conv-r", deathmatch_reflections=[])


def _fill_same_signature(mem: ReflectionMemory, n: int = 3):
    for i in range(n):
        mem.add(turn=i + 1, action_summary=f"尝试{i+1}", verdict="partial",
                issues=["文件未生成"], retry_instruction="产出文件")


def test_add_assigns_id_and_not_retracted():
    mem = ReflectionMemory(_conv())
    e = mem.add(turn=1, action_summary="a", verdict="partial", issues=["x"])
    assert e.get("id"), "条目必须带 id（A5）"
    assert e.get("retracted") in (False, None)


def test_retract_repeated_marks_and_renders_avoid_block():
    conv = _conv()
    mem = ReflectionMemory(conv)
    _fill_same_signature(mem)
    n = mem.retract_repeated(window=3, note="该路线已被证伪")
    assert n == 3, "同签名窗口 3 条应全部撤回"
    assert all(e.get("retracted") for e in conv.deathmatch_reflections)
    prompt = mem.build_injection_prompt()
    assert "避免" in prompt, "撤回条目必须渲染为「避免」块"
    assert "该路线已被证伪" in prompt


def test_detect_ignores_retracted_after_retract():
    conv = _conv()
    mem = ReflectionMemory(conv)
    _fill_same_signature(mem)
    assert mem.detect_repeated_failures(window=3), "撤回前应触发升级"
    mem.retract_repeated(window=3, note="证伪")
    assert mem.detect_repeated_failures(window=3) is None, "撤回后升级窗口须重算（不重复触发）"


def test_maybe_retract_on_escalation_respects_switch(monkeypatch):
    conv = _conv()
    mem = ReflectionMemory(conv)
    _fill_same_signature(mem)
    dm = refl.config._config.setdefault("deathmatch", {})
    prev = dm.get("reflection_retract_enabled")
    dm["reflection_retract_enabled"] = False
    try:
        assert mem.maybe_retract_on_escalation(window=3) == 0, "默认关不得自动撤回"
        dm["reflection_retract_enabled"] = True
        assert mem.maybe_retract_on_escalation(window=3) == 3
    finally:
        if prev is None:
            dm.pop("reflection_retract_enabled", None)
        else:
            dm["reflection_retract_enabled"] = prev


def test_legacy_entries_without_id_still_render():
    conv = SimpleNamespace(id="conv-old", deathmatch_reflections=[
        {"turn": 1, "action_summary": "旧条目", "issues": ["y"], "retry_instruction": ""},
    ])
    mem = ReflectionMemory(conv)
    prompt = mem.build_injection_prompt()
    assert "旧条目" in prompt
    assert mem.detect_repeated_failures(window=3) is None


def test_retract_persists_across_orm_sessions():
    """复审 I-persistence：撤回态必须真落库（同对象重赋值=no-op 陷阱 pin）。
    用真 ORM 会话（仿 test_stream_persistence sqlite 桥），杜绝 SimpleNamespace 假绿。"""
    from sqlalchemy import BigInteger, create_engine, select
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

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__, Conversation.__table__])
    with Session(engine) as s:
        u = User(username="rr", password_hash="x")
        s.add(u)
        s.flush()
        c = Conversation(user_id=u.id, title="t", deathmatch_reflections=[])
        s.add(c)
        s.commit()
        conv_id = c.id
    # 阶段 1：写入并提交（清空脏标记——否则 add 的脏状态会搭车把
    # 原地撤回改动一起冲走 = 空真，复审实锤）
    with Session(engine) as s:
        conv = s.get(Conversation, conv_id)
        _fill_same_signature(ReflectionMemory(conv))
        s.commit()
    # 阶段 2：干净会话撤回 + 提交（判别面：撤回自身必须制造脏状态）
    with Session(engine) as s:
        conv = s.get(Conversation, conv_id)
        assert ReflectionMemory(conv).retract_repeated(window=3, note="证伪") == 3
        s.commit()
    with Session(engine) as s:
        conv = s.get(Conversation, conv_id)
        entries = conv.deathmatch_reflections or []
        assert entries and all(e.get("retracted") for e in entries), (
            "撤回标记必须落库（同对象重赋值陷阱回归）")
        assert "避免" in ReflectionMemory(conv).build_injection_prompt()
