"""W3-3/W3-4（记忆/长程 v2，2026-10-23，判据 #17）：probe→hit/miss→只修 miss
→ 报告（hit@k 前后 + cost_tokens）；默认关；14 天无正收益自动停。"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestDreamRepairPG:
    def test_probe_repair_and_stop_rule(self):
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
                                         ConversationGroup, DreamRepairReport,
                                         MemoryConcept, MemoryEvalRun, Message,
                                         User)
            from app.services import memory_dream_repair_service as drs

            eng = create_async_engine(dsn)
            try:
                async with eng.begin() as conn:
                    await conn.run_sync(lambda c: Base.metadata.create_all(
                        c, tables=[Base.metadata.tables[n] for n in
                                   ("users", "conversation_groups", "conversations",
                                    "messages", "memory_concepts",
                                    "dream_repair_reports",
                                    "memory_eval_runs")], checkfirst=True))
            except Exception as exc:
                pytest.skip(f"PG unavailable: {type(exc).__name__}")
            from sqlalchemy.ext.asyncio import async_sessionmaker
            Session = async_sessionmaker(eng, expire_on_commit=False)
            uid = "drep-" + os.urandom(4).hex()
            try:
                async with Session() as db:
                    db.add(User(id=uid, username=uid, password_hash="x"))
                    db.add(ConversationGroup(id="g" + uid, user_id=uid, name="g"))
                    await db.flush()
                    db.add(Conversation(id="c" + uid, user_id=uid, title="t",
                                        group_id="g" + uid))
                    await db.flush()
                    db.add(MemoryConcept(user_id=uid, canonical_name="项目A",
                                         description_short="s", weight=0.9))
                    db.add(Message(conversation_id="c" + uid, role="user",
                                   content="请整理「项目A」和「项目B」的资料"))
                    await db.commit()
                # 默认关：不落行
                async with Session() as db:
                    assert await drs.run_probe_repair(db, uid) is None
                    await db.commit()
                    n = (await db.execute(select(DreamRepairReport).where(
                        DreamRepairReport.user_id == uid))).scalars().all()
                    assert n == []
                # enabled：探测（项目A=hit）→ 修 miss → 报告
                async with Session() as db:
                    out = await drs.run_probe_repair(db, uid, enabled=True,
                                                     probe_count=2)
                    await db.commit()
                    assert out is not None
                    assert out["probe_count"] == 2, out
                    assert out["miss_count"] == 1, "「项目B」应为缺口（可不命中）"
                    assert out["hit_at_k_before"] == 0.5
                    assert out["hit_at_k_after"] == 1.0
                    assert out["cost_tokens"] == 0
                    rows = (await db.execute(select(DreamRepairReport).where(
                        DreamRepairReport.user_id == uid))).scalars().all()
                    assert len(rows) == 1
                # 零增益 → 停止标记 → 再跑 None（W3-4）
                async with Session() as db:
                    rows = (await db.execute(select(DreamRepairReport).where(
                        DreamRepairReport.user_id == uid))).scalars().all()
                    for r in rows:
                        r.hit_at_k_before = 1.0
                        r.hit_at_k_after = 0.0
                    await db.commit()
                async with Session() as db:
                    assert await drs.check_stop_rule(db, uid) is True
                    await db.commit()
                async with Session() as db:
                    assert await drs.run_probe_repair(db, uid, enabled=True) is None
                    await db.commit()
            finally:
                async with Session() as db:
                    await db.execute(delete(MemoryEvalRun).where(
                        MemoryEvalRun.metrics_json.cast(type_=db.bind.dialect.name
                                                        and __import__("sqlalchemy").Text)
                        .like(f"%{uid}%")))
                    await db.execute(delete(Message).where(
                        Message.conversation_id == "c" + uid))
                    await db.execute(delete(Conversation).where(
                        Conversation.id == "c" + uid))
                    await db.execute(delete(ConversationGroup).where(
                        ConversationGroup.id == "g" + uid))
                    await db.execute(delete(DreamRepairReport).where(
                        DreamRepairReport.user_id == uid))
                    await db.execute(delete(MemoryConcept).where(
                        MemoryConcept.user_id == uid))
                    await db.execute(delete(User).where(User.id == uid))
                    await db.commit()
                await eng.dispose()

        asyncio.run(_run())
