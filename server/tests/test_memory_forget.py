"""W0-4/W5-1/W5-2 遗忘闭环（记忆/长程 v2，2026-10-23，判据 #5/#22/#23）。

D1=a memory-only：删记忆层，保留对话/笔记（API/UI 明示保留面）；
清单（deletion_manifests）+ 残余扫描 + 六类抽取测试。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_memory_forget.py -v
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestParseForgetNeedles:
    def test_quoted_terms(self):
        from app.services.memory_forget_service import parse_forget_needles

        out = parse_forget_needles("请忘掉「项目灯塔」和「Q3 预算」")
        assert "项目灯塔" in out and "Q3 预算" in out

    def test_explicit_marker(self):
        from app.services.memory_forget_service import parse_forget_needles

        out = parse_forget_needles("删除记忆：我的家庭住址")
        assert out == ["我的家庭住址"]

    def test_empty_and_no_terms(self):
        from app.services.memory_forget_service import parse_forget_needles

        assert parse_forget_needles("") == []
        assert parse_forget_needles("你好") == []


class TestResidualReport:
    def test_shape_memory_only_disclosure(self):
        from app.services.memory_forget_service import residual_report

        rep = residual_report(needles=["X"], hits={"conversations": 2, "notes": 1},
                              memory_hits={})
        assert rep["semantic"] == "memory-only"
        assert "conversations" in rep["retained_surfaces_declared"]
        assert rep["residual_count"] == 0, "memory-only 口径残余=0"
        assert rep["retained_hits"]["conversations"] == 2

    def test_memory_residual_counts(self):
        from app.services.memory_forget_service import residual_report

        rep = residual_report(needles=["X"], hits={}, memory_hits={"memory_concepts": 1})
        assert rep["residual_count"] == 1


class TestForgetService:
    """六类抽取测试（Bemula 式）——PG 集成（无 PG 时 skip）。"""

    def test_six_request_types(self):
        dsn = os.environ.get("WT_TEST_PG_URL", "").strip()
        try:
            from app.core.config import get_config
            dsn = dsn or get_config().database_url
        except Exception:
            pass
        if not dsn:
            pytest.skip("no PG DSN")
        from sqlalchemy.ext.asyncio import create_async_engine

        async def _run():
            from sqlalchemy import delete, select, text

            from app.db.database import (AgentDream, AgentMemory, Base,
                                         Conversation, ConversationGroup,
                                         DeletionManifest, MemoryConcept,
                                         User, UserAgentState)
            from app.services import memory_forget_service as mfs

            eng = create_async_engine(dsn)
            try:
                async with eng.begin() as conn:
                    await conn.run_sync(lambda c: Base.metadata.create_all(
                        c, tables=[Base.metadata.tables[n] for n in (
                            "users", "conversation_groups", "conversations",
                            "user_agent_states", "memory_concepts",
                            "agent_memories", "agent_dreams",
                            "deletion_manifests")], checkfirst=True))
            except Exception as exc:
                pytest.skip(f"PG unavailable: {type(exc).__name__}")
            from sqlalchemy.ext.asyncio import async_sessionmaker
            Session = async_sessionmaker(eng, expire_on_commit=False)
            uid = "forget-" + os.urandom(4).hex()
            try:
                async with Session() as db:
                    db.add(User(id=uid, username=uid, password_hash="x"))
                    db.add(ConversationGroup(id="g" + uid, user_id=uid, name="g"))
                    await db.flush()
                    st = UserAgentState(user_id=uid, memory_summary="s",
                                        dream_summary="d")
                    db.add(st)
                    await db.flush()
                    # 目标事实 + 旁证事实
                    db.add(MemoryConcept(user_id=uid, canonical_name="项目灯塔",
                                         description_short="保密项目",
                                         description_full="项目灯塔的细节"))
                    db.add(MemoryConcept(user_id=uid, canonical_name="Q3 预算",
                                         description_short="旁证事实",
                                         description_full="无关旁证"))
                    db.add(AgentMemory(agent_state_id=st.id, content="项目灯塔要删除"))
                    db.add(AgentDream(agent_state_id=st.id, generated_for_date="2026-10-23",
                                      summary="梦见项目灯塔"))
                    await db.commit()

                # ① 精确删除 + ⑤ 无越权（另建用户不受影响）
                uid2 = "other-" + os.urandom(4).hex()
                async with Session() as db:
                    db.add(User(id=uid2, username=uid2, password_hash="x"))
                    await db.flush()
                    db.add(MemoryConcept(user_id=uid2, canonical_name="项目灯塔",
                                         description_short="他人同名",
                                         description_full="他人事实"))
                    await db.commit()
                async with Session() as db:
                    manifest = await mfs.create_deletion_manifest(
                        db, uid, "req-1", {"needles": ["项目灯塔"]})
                    await db.commit()
                    mid = manifest.id
                async with Session() as db:
                    rep = await mfs.execute_forget(db, uid, mid)
                    await db.commit()
                assert rep["residual_count"] == 0, f"memory-only 口径残余=0，got {rep}"
                assert rep["semantic"] == "memory-only"

                async with Session() as db:
                    gone = (await db.execute(select(MemoryConcept).where(
                        MemoryConcept.user_id == uid,
                        MemoryConcept.canonical_name == "项目灯塔"))).scalars().all()
                    assert gone == [] or all(c.status == "forgotten" for c in gone)
                # ② 旁证保留
                async with Session() as db:
                    keep = (await db.execute(select(MemoryConcept).where(
                        MemoryConcept.user_id == uid,
                        MemoryConcept.canonical_name == "Q3 预算"))).scalars().all()
                    assert len(keep) == 1, "旁证事实必须保留"
                # ⑤ 他人数据不动
                async with Session() as db:
                    other = (await db.execute(select(MemoryConcept).where(
                        MemoryConcept.user_id == uid2))).scalars().all()
                    assert len(other) == 1, "无越权删除"
                # ④ 抗抽取（必须在再学习之前）：memory 层残余扫描为 0
                # （对话/笔记=声明保留面）
                async with Session() as db:
                    rep2 = await mfs.scan_residuals(db, uid, ["项目灯塔"])
                    assert rep2["memory"] == {}, f"抗抽取：memory 层残余为空，got {rep2}"
                # ③ 再学习：同名概念可重新建立
                async with Session() as db:
                    db.add(MemoryConcept(user_id=uid, canonical_name="项目灯塔",
                                         description_short="重新学习",
                                         description_full="新事实"))
                    await db.commit()
                    rows = (await db.execute(select(MemoryConcept).where(
                        MemoryConcept.user_id == uid,
                        MemoryConcept.canonical_name == "项目灯塔"))).scalars().all()
                    assert rows, "再学习必须可行"
                # ⑥ 清单审计（不含正文）
                async with Session() as db:
                    man = await db.get(DeletionManifest, mid)
                    assert man is not None and man.status == "completed"
                    assert man.residual_json is not None
            finally:
                async with Session() as db:
                    await db.execute(delete(DeletionManifest).where(
                        DeletionManifest.user_id.in_([uid, uid2])))
                    await db.execute(delete(AgentDream).where(
                        AgentDream.agent_state_id == select(UserAgentState.id).where(
                            UserAgentState.user_id == uid).scalar_subquery()))
                    await db.execute(delete(AgentMemory).where(
                        AgentMemory.agent_state_id == select(UserAgentState.id).where(
                            UserAgentState.user_id == uid).scalar_subquery()))
                    await db.execute(delete(MemoryConcept).where(
                        MemoryConcept.user_id.in_([uid, uid2])))
                    await db.execute(delete(UserAgentState).where(
                        UserAgentState.user_id == uid))
                    await db.execute(delete(Conversation).where(
                        Conversation.user_id.in_([uid, uid2])))
                    await db.execute(delete(ConversationGroup).where(
                        ConversationGroup.user_id == uid))
                    await db.execute(delete(User).where(User.id.in_([uid, uid2])))
                    await db.commit()
                await eng.dispose()

        asyncio.run(_run())


class TestEraseExtendedSurfaces:
    """#22 W5-1 擦除面：artifacts/rolling summaries/resume snapshots/evidence
    units/skills/pinned constraints/dream reports 全部按 user_id 作用域。"""

    def test_erase_extended_surfaces_scoped(self):
        dsn = os.environ.get("WT_TEST_PG_URL", "").strip()
        try:
            from app.core.config import get_config
            dsn = dsn or get_config().database_url
        except Exception:
            pass
        if not dsn:
            pytest.skip("no PG DSN")
        from sqlalchemy.ext.asyncio import create_async_engine

        async def _run():
            from sqlalchemy import delete, select

            from app.db.database import (Base, Conversation,
                                         ConversationPinnedConstraint,
                                         ConversationGroup, DeletionManifest,
                                         DreamRepairReport, SkillFile, User,
                                         UserSkill)
            from app.services import memory_forget_service as mfs

            eng = create_async_engine(dsn)
            try:
                async with eng.begin() as conn:
                    await conn.run_sync(lambda c: Base.metadata.create_all(
                        c, tables=[Base.metadata.tables[n] for n in (
                            "users", "conversation_groups", "conversations",
                            "user_skills", "skill_files",
                            "conversation_pinned_constraints",
                            "dream_repair_reports", "deletion_manifests",
                            "step_checkpoints", "memory_eval_runs")],
                        checkfirst=True))
            except Exception as exc:
                pytest.skip(f"PG unavailable: {type(exc).__name__}")
            from sqlalchemy.ext.asyncio import async_sessionmaker
            Session = async_sessionmaker(eng, expire_on_commit=False)
            uid = "erase-" + os.urandom(4).hex()
            cid = "ec-" + uid
            other = "erase2-" + os.urandom(4).hex()
            try:
                async with Session() as db:
                    db.add(User(id=uid, username=uid, password_hash="x"))
                    db.add(User(id=other, username=other, password_hash="x"))
                    db.add(ConversationGroup(id="g" + uid, user_id=uid, name="g"))
                    db.add(UserSkill(user_id=uid, name="sk", content="c"))
                    db.add(UserSkill(user_id=other, name="sk2", content="c"))
                    await db.flush()
                    db.add(Conversation(id=cid, user_id=uid, title="t",
                                        group_id="g" + uid))
                    await db.commit()
                async with Session() as db:
                    db.add(ConversationPinnedConstraint(
                        user_id=uid, conversation_id=cid, text="约束A"))
                    db.add(DreamRepairReport(user_id=uid, run_date="2026-10-23"))
                    await db.commit()
                async with Session() as db:
                    counts = await mfs.erase_extended_surfaces(db, uid)
                    await db.commit()
                assert counts.get("pinned_constraints", 0) == 1
                assert counts.get("dream_repair_reports", 0) == 1
                assert counts.get("user_skills", 0) == 1
                async with Session() as db:
                    other_skills = (await db.execute(select(UserSkill).where(
                        UserSkill.user_id == other))).scalars().all()
                    assert len(other_skills) == 1, "无越权删除"
                    pins = (await db.execute(select(ConversationPinnedConstraint).where(
                        ConversationPinnedConstraint.user_id == uid))).scalars().all()
                    assert pins == []
            finally:
                async with Session() as db:
                    for model, col in ((ConversationPinnedConstraint, "user_id"),
                                       (DreamRepairReport, "user_id"),
                                       (UserSkill, "user_id"),
                                       (Conversation, "user_id"),
                                       (DeletionManifest, "user_id"),
                                       (ConversationGroup, "user_id")):
                        await db.execute(delete(model).where(
                            getattr(model, col).in_([uid, other])))
                    await db.execute(delete(User).where(User.id.in_([uid, other])))
                    await db.commit()
                await eng.dispose()

        asyncio.run(_run())
