"""W4-1 死磕步骤对齐回退（记忆/长程 v2，2026-10-23，判据 #18）。

判据 #18：三元组 (step_id, context_cursor, snapshot_id) 落库；
rewind 到第 N 步不重复已完成步——steps_discarded 里是其后需要重跑的步。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_step_checkpoints.py -v
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestStepCheckpointRewind:
    """PG 集成：WT_TEST_PG_URL 优先、连接失败 pytest.skip、
    自建 engine + create_all(checkfirst) + 清理。"""

    def test_record_and_rewind(self):
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
            from sqlalchemy import delete

            from app.db.database import (Base, Conversation, ConversationGroup,
                                         StepCheckpoint, User)
            from app.services import deathmatch_step_checkpoint_service as scs

            eng = create_async_engine(dsn)
            try:
                async with eng.begin() as conn:
                    await conn.run_sync(lambda c: Base.metadata.create_all(
                        c, tables=[Base.metadata.tables[n] for n in (
                            "users", "assistants", "conversation_groups",
                            "conversations", "step_checkpoints")],
                        checkfirst=True))
            except Exception as exc:
                pytest.skip(f"PG unavailable: {type(exc).__name__}")
            from sqlalchemy.ext.asyncio import async_sessionmaker
            Session = async_sessionmaker(eng, expire_on_commit=False)
            uid = "w41-" + os.urandom(4).hex()
            cid = "w41c-" + os.urandom(4).hex()
            try:
                async with Session() as db:
                    db.add(User(id=uid, username=uid, password_hash="x"))
                    db.add(ConversationGroup(id="g" + uid, user_id=uid, name="g"))
                    await db.flush()
                    db.add(Conversation(id=cid, user_id=uid, title="t",
                                        group_id="g" + uid))
                    await db.commit()

                # ① 三元组落库：3 步 checkpoint（cursor 10/20/30）
                async with Session() as db:
                    r1 = await scs.record_step_checkpoint(
                        db, cid, "step-1", 10, "snap-1")
                    assert r1.id, "落库行必须有主键"
                    assert r1.step_id == "step-1"
                    assert r1.context_cursor == 10
                    assert r1.snapshot_id == "snap-1"
                    await scs.record_step_checkpoint(db, cid, "step-2", 20, "snap-2")
                    await scs.record_step_checkpoint(db, cid, "step-3", 30, "snap-3")

                # ② rewind 到第 2 步：丢弃第 3 步（=需重跑），不重复已完成步
                async with Session() as db:
                    out = await scs.rewind_to_step(db, cid, "step-2")
                assert out["restored"] is True
                assert out["step_id"] == "step-2"
                assert out["context_cursor"] == 20
                assert out["snapshot_id"] == "snap-2"
                assert out["steps_discarded"] == ["step-3"], \
                    f"其后只有 step-3 需重跑，got {out}"

                # ③ rewind 到最后一步：没有需要重跑的步
                async with Session() as db:
                    out3 = await scs.rewind_to_step(db, cid, "step-3")
                assert out3["restored"] is True
                assert out3["steps_discarded"] == []

                # ④ 不存在的步 → restored=False
                async with Session() as db:
                    miss = await scs.rewind_to_step(db, cid, "step-9")
                assert miss["restored"] is False
                assert miss["reason"] == "no checkpoint"

                # ⑤ 同 (deathmatch_id, step_id) 可重复记录，取最新
                async with Session() as db:
                    await scs.record_step_checkpoint(
                        db, cid, "step-2", 25, "snap-2b")
                async with Session() as db:
                    latest = await scs.rewind_to_step(db, cid, "step-2")
                assert latest["context_cursor"] == 25, "重复记录取最新"
                assert latest["snapshot_id"] == "snap-2b"
                assert latest["steps_discarded"] == ["step-3"]

                # ⑥ 未落库（未调用 record）前不存在的 deathmatch_id 也应 restored=False
                async with Session() as db:
                    other = await scs.rewind_to_step(db, "w41-ghost", "step-1")
                assert other["restored"] is False
            finally:
                async with Session() as db:
                    await db.execute(delete(StepCheckpoint).where(
                        StepCheckpoint.deathmatch_id.in_([cid, "w41-ghost"])))
                    await db.execute(delete(Conversation).where(
                        Conversation.user_id == uid))
                    await db.execute(delete(ConversationGroup).where(
                        ConversationGroup.user_id == uid))
                    await db.execute(delete(User).where(User.id == uid))
                    await db.commit()
                await eng.dispose()

        asyncio.run(_run())
