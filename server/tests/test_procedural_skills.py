"""W4-3 程序性记忆（记忆/长程 v2，2026-10-23，判据 #20）。

判据 #20：deathmatch_reflections → 候选技能（人工门）→ user_skills 落库；
技能池上限 30 拒绝。候选只在内存中拼装（确定性，不调 LLM、不落库），
人工门 approve 才落库。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_procedural_skills.py -v
"""
import asyncio
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _refl(i, turn, summary, verdict="ok", issues=None, retry="", retracted=False):
    return {"id": i, "turn": turn, "action_summary": summary, "verdict": verdict,
            "issues": issues or [], "retry_instruction": retry, "retracted": retracted}


SAMPLE = [
    _refl("r1", 3, "部署服务到生产环境", "ok", ["端口占用"], "先检查端口"),
    _refl("r2", 5, "部署脚本回滚演练", "retry", ["镜像 tag 混乱"], "锁定 tag"),
    _refl("r3", 7, "部署已被覆盖的产物", "bad", [], "", retracted=True),  # 撤回条目不进候选
    _refl("r4", 4, "撰写项目报告", "ok", ["缺数据来源"], "先补数据源"),
]


class TestBuildCandidates:
    """纯函数：拼装确定性（同输入→同输出，含候选 id）。"""

    def test_retracted_excluded_and_grouped(self):
        from app.services.memory_procedural_service import build_skill_candidates

        cands = build_skill_candidates(SAMPLE, "user-a", limit=10)
        names = [c["name"] for c in cands]
        assert "部署技能" in names and "报告技能" in names
        deploy = next(c for c in cands if c["name"] == "部署技能")
        assert "r3" not in deploy["source_reflections"], "retracted=True 条目必须排除"
        assert set(deploy["source_reflections"]) == {"r1", "r2"}
        assert "部署服务到生产环境" in deploy["content"]
        assert "端口占用" in deploy["content"], "issues 进经验条目"
        assert "先检查端口" in deploy["content"], "retry_instruction 进经验条目"

    def test_candidate_id_sha1(self):
        from app.services.memory_procedural_service import build_skill_candidates

        cands = build_skill_candidates(SAMPLE, "user-a", limit=10)
        for c in cands:
            expect = hashlib.sha1(("user-a" + c["name"]).encode("utf-8")).hexdigest()[:12]
            assert c["id"] == expect, "候选 id = sha1(user_id + 名称)[:12]"
        assert all(set(c) == {"id", "name", "description", "content",
                              "source_reflections"} for c in cands)

    def test_deterministic_regardless_of_input_order(self):
        from app.services.memory_procedural_service import build_skill_candidates

        a = build_skill_candidates(SAMPLE, "user-a", limit=10)
        b = build_skill_candidates(list(reversed(SAMPLE)), "user-a", limit=10)
        assert [(c["id"], c["name"], c["content"]) for c in a] == \
               [(c["id"], c["name"], c["content"]) for c in b], \
            "同输入（不同顺序）必须产出完全一致的候选"

    def test_limit_and_empty(self):
        from app.services.memory_procedural_service import build_skill_candidates

        assert build_skill_candidates([], "user-a") == []
        assert build_skill_candidates([_refl("x", 1, "", "ok")], "user-a") == []
        one = build_skill_candidates(SAMPLE, "user-a", limit=1)
        assert len(one) == 1


class TestProceduralApproval:
    """PG 集成：approve 落 user_skills、幂等、池满拒绝、候选不存在。"""

    def test_harvest_approve_pool(self):
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
            from sqlalchemy import delete, func, select

            from app.db.database import (Base, Conversation, ConversationGroup,
                                         User, UserSkill)
            from app.services import memory_procedural_service as mps

            eng = create_async_engine(dsn)
            try:
                async with eng.begin() as conn:
                    await conn.run_sync(lambda c: Base.metadata.create_all(
                        c, tables=[Base.metadata.tables[n] for n in (
                            "users", "assistants", "conversation_groups",
                            "conversations", "user_skills", "skill_files")],
                        checkfirst=True))
            except Exception as exc:
                pytest.skip(f"PG unavailable: {type(exc).__name__}")
            from sqlalchemy.ext.asyncio import async_sessionmaker
            Session = async_sessionmaker(eng, expire_on_commit=False)
            uid = "w43-" + os.urandom(4).hex()
            cid = "w43c-" + os.urandom(4).hex()
            try:
                async with Session() as db:
                    db.add(User(id=uid, username=uid, password_hash="x"))
                    db.add(ConversationGroup(id="g" + uid, user_id=uid, name="g"))
                    await db.flush()
                    db.add(Conversation(id=cid, user_id=uid, title="t",
                                        group_id="g" + uid,
                                        deathmatch_reflections=SAMPLE))
                    await db.commit()

                # ① harvest：reflections（retracted=False）→ 候选（不落库）
                async with Session() as db:
                    cands = await mps.harvest_skill_candidates(db, uid)
                names = [c["name"] for c in cands]
                assert "部署技能" in names and "报告技能" in names
                async with Session() as db:
                    cnt = (await db.execute(select(func.count()).select_from(
                        UserSkill).where(UserSkill.user_id == uid))).scalar()
                assert cnt == 0, "候选不落库（人工门前零副作用）"

                deploy = next(c for c in cands if c["name"] == "部署技能")

                # ② 人工门通过 → UserSkill(is_active=True) 落库
                async with Session() as db:
                    skill, reason = await mps.approve_skill_candidate(db, uid, deploy["id"])
                    await db.commit()
                assert reason == "created"
                assert skill is not None and skill.is_active is True
                assert skill.name == "部署技能"
                assert skill.user_id == uid

                # ③ skill_pool_usage
                async with Session() as db:
                    usage = await mps.skill_pool_usage(db, uid)
                assert usage == {"used": 1, "cap": 30}

                # ④ 幂等：同名 skill 已存在 → 返回已有行
                async with Session() as db:
                    again, reason2 = await mps.approve_skill_candidate(
                        db, uid, deploy["id"])
                    await db.commit()
                assert reason2 == "exists"
                assert again is not None and again.id == skill.id
                async with Session() as db:
                    usage2 = await mps.skill_pool_usage(db, uid)
                assert usage2["used"] == 1, "幂等：不得重复落库"

                # ⑤ 候选不存在 → (None, "no_candidate")
                async with Session() as db:
                    none_skill, reason3 = await mps.approve_skill_candidate(
                        db, uid, "deadbeef1234")
                assert none_skill is None and reason3 == "no_candidate"

                # ⑥ 池上限 30：补满后拒绝（返回 None 并 reason="pool_full"）
                report = next(c for c in cands if c["name"] == "报告技能")
                async with Session() as db:
                    for i in range(29):
                        db.add(UserSkill(user_id=uid, name=f"filler-{i}",
                                         content="c", is_active=True))
                    await db.commit()
                async with Session() as db:
                    full_skill, reason4 = await mps.approve_skill_candidate(
                        db, uid, report["id"])
                    await db.commit()
                assert full_skill is None and reason4 == "pool_full", \
                    "池满必须拒绝（上限 30）"
                async with Session() as db:
                    usage3 = await mps.skill_pool_usage(db, uid)
                assert usage3 == {"used": 30, "cap": 30}
            finally:
                async with Session() as db:
                    await db.execute(delete(UserSkill).where(
                        UserSkill.user_id == uid))
                    await db.execute(delete(Conversation).where(
                        Conversation.user_id == uid))
                    await db.execute(delete(ConversationGroup).where(
                        ConversationGroup.user_id == uid))
                    await db.execute(delete(User).where(User.id == uid))
                    await db.commit()
                await eng.dispose()

        asyncio.run(_run())
