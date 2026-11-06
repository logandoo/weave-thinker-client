"""W5-3（记忆/长程 v2，2026-10-23，判据 #24）：retention_score + 容量淘汰
shadow 预览 + 撤销（D5=a：按容量 + 预览，默认 shadow 只记录不删）。"""
import asyncio
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestScore:
    def test_formula_and_decay(self):
        from app.services.memory_retention_service import retention_score_for

        now = datetime(2026, 10, 23)
        fresh = retention_score_for(1.0, 14.0, 10, now, now, now=now)
        old = retention_score_for(1.0, 14.0, 10,
                                  now - timedelta(days=30), now - timedelta(days=30),
                                  now=now)
        assert fresh > old
        assert 0.0 <= old <= 1.0

    def test_no_timestamps(self):
        from app.services.memory_retention_service import retention_score_for

        v = retention_score_for(0.5, 14.0, 0, None, None)
        assert 0.0 < v <= 1.0


class TestPreviewPG:
    def test_shadow_preview_undo(self):
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

            from app.db.database import Base, MemoryConcept, User
            from app.services import memory_retention_service as rs

            eng = create_async_engine(dsn)
            try:
                async with eng.begin() as conn:
                    await conn.run_sync(lambda c: Base.metadata.create_all(
                        c, tables=[Base.metadata.tables[n] for n in
                                   ("users", "memory_concepts", "memory_eval_runs")],
                        checkfirst=True))
            except Exception as exc:
                pytest.skip(f"PG unavailable: {type(exc).__name__}")
            from sqlalchemy.ext.asyncio import async_sessionmaker
            Session = async_sessionmaker(eng, expire_on_commit=False)
            uid = "reten-" + os.urandom(4).hex()
            try:
                async with Session() as db:
                    db.add(User(id=uid, username=uid, password_hash="x"))
                    await db.flush()
                    for i in range(5):
                        db.add(MemoryConcept(
                            user_id=uid, canonical_name=f"c{i}",
                            description_short="s", weight=0.1 * i))
                    await db.commit()
                async with Session() as db:
                    rep = await rs.retention_preview_report(db, uid, capacity=3)
                    assert rep["active"] == 5
                    assert len(rep["candidates"]) == 2, "超容量 2 条候选"
                async with Session() as db:
                    out = await rs.apply_eviction(db, uid, 3, mode="shadow")
                    await db.commit()
                    assert out["applied"] is False
                    rows = (await db.execute(select(MemoryConcept).where(
                        MemoryConcept.user_id == uid,
                        MemoryConcept.fidelity_state == "archived"))).scalars().all()
                    assert rows == [], "shadow 不得落 archived"
                async with Session() as db:
                    out = await rs.apply_eviction(db, uid, 3, mode="enforce")
                    await db.commit()
                    assert out["applied"] is True
                    rows = (await db.execute(select(MemoryConcept).where(
                        MemoryConcept.user_id == uid,
                        MemoryConcept.fidelity_state == "archived"))).scalars().all()
                    assert len(rows) == 2
                async with Session() as db:
                    n = await rs.undo_eviction(db, uid, [r.id for r in rows])
                    await db.commit()
                    assert n == 2, "撤销恢复 full"
            finally:
                async with Session() as db:
                    await db.execute(delete(MemoryConcept).where(
                        MemoryConcept.user_id == uid))
                    await db.execute(delete(User).where(User.id == uid))
                    await db.commit()
                await eng.dispose()

        asyncio.run(_run())
