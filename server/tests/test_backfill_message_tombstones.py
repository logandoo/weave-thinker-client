"""RED tests (A4.8) — P3 一次性 backfill：孤儿 create 补 delete 事件 + 墓碑（UPSTREAM_TODO_20261031 §5.1，2026-10-31）。

存量事故：P0 修复前 `_delete_message_slice` 走 Core 批量 DELETE，零 delete 事件/墓碑，
客户端只收到过消息的 create → 永久残留。P3 用 `sync_events` 残留的 messages/create
反查「messages 行不存在、无墓碑、会话仍存在」的 id，补 delete + tombstone，
让所有客户端 delta/resync 后收敛。

契约（工单 §5.1 / acceptance #8）：
1. find：只命中「有 create 事件 + 消息行不存在 + 无墓碑 + 会话存在 + payload 带
   conversation_id」的 id；消息在/墓碑在/会话删了/payload 无 cid → 都跳过；
   同 id 多条 create 去重；--since 过滤旧事件。
2. apply：dry-run 零写入；execute 写 delete 事件（payload=NULL、origin_device=NULL、
   user=事件 user）+ 墓碑；每批一个提交点（分批 500）。
3. 幂等：execute 后再 find 为空；重复 apply 不产生重复事件/墓碑。
4. fail-safe：find 与 apply 之间消息行复活（同 id 重新出现）→ apply 批内复查跳过。

Run: cd backend && python -m pytest tests/test_backfill_message_tombstones.py -v
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import BigInteger, create_engine, event as sa_event, select  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_as_json(element, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_as_int(element, compiler, **kw):
    return "INTEGER"


def _run(coro):
    return asyncio.run(coro)


class _Bridge:
    """asyncio 鸭子壳包真实 sync Session（与 test_edit_resend_delete_events 同缝）。"""

    def __init__(self, sync_sess):
        self._s = sync_sess

    async def execute(self, stmt, *a, **k):
        return self._s.execute(stmt, *a, **k)

    async def commit(self):
        self._s.commit()

    async def rollback(self):
        self._s.rollback()


def _env():
    from app.db.database import (
        Base, ChatSession, Conversation, ConversationGroup, Message,
        MessagePayload, SubconsciousLog, SyncEvent, SyncTombstone,
    )

    engine = create_engine("sqlite://", poolclass=StaticPool)

    tables, stack, seen = [], [
        ConversationGroup.__table__, Conversation.__table__, Message.__table__,
        MessagePayload.__table__, SyncEvent.__table__, SyncTombstone.__table__,
        SubconsciousLog.__table__, ChatSession.__table__,
    ], set()
    while stack:
        t = stack.pop()
        if t.name in seen:
            continue
        seen.add(t.name)
        tables.append(t)
        for col in t.columns:
            for fk in col.foreign_keys:
                stack.append(fk.column.table)
    Base.metadata.create_all(engine, tables=tables)
    return engine


_BASE = datetime(2026, 10, 30, 12, 0, 0)


def _seed(engine, *, owner="owner"):
    """c1 存在（owner）；c_gone 已删（事件残留）；消息行全部不在。

    create 事件布局（seq 递增）：
      1 a1@c1 孤儿            → 命中
      2 a1@c1 重复 create     → 去重不重复计
      3 b1@c1 消息仍在        → 跳过
      4 g1@c_gone 会话已删    → 跳过
      5 n1 payload 无 cid     → 跳过
      6 t1 已有墓碑           → 跳过
      7 old1 事件很旧         → --since 过滤
    """
    from app.db.database import (
        Conversation, Message, SyncEvent, SyncTombstone, User,
    )
    with Session(engine) as s:
        s.add(User(id=owner, username="o", password_hash="x"))
        s.add(Conversation(id="c1", user_id=owner, title="t"))
        ev = lambda i, eid, cid, mins: SyncEvent(  # noqa: E731
            user_id=owner, entity_type="messages", entity_id=eid, op="create",
            payload={"conversation_id": cid} if cid else {"content": "x"},
            created_at=_BASE + timedelta(minutes=mins),
        )
        s.add_all([
            ev(1, "a1", "c1", 0),
            ev(2, "a1", "c1", 1),
            ev(3, "b1", "c1", 2),
            ev(4, "g1", "c_gone", 3),
            ev(5, "n1", None, 4),
            ev(6, "t1", "c1", 5),
            ev(7, "old1", "c1", -60 * 24 * 30),
            # update/delete 事件不参与反查
            SyncEvent(user_id=owner, entity_type="messages", entity_id="u9",
                      op="update", payload={"conversation_id": "c1"},
                      created_at=_BASE),
            SyncEvent(user_id=owner, entity_type="notes", entity_id="note1",
                      op="create", payload={}, created_at=_BASE),
        ])
        # b1 消息行仍在
        s.add(Message(id="b1", conversation_id="c1", role="assistant",
                      content="还在", created_at=_BASE))
        # t1 已有墓碑
        s.add(SyncTombstone(user_id=owner, entity_type="messages",
                            entity_id="t1", deleted_at=_BASE))
        s.commit()


def _find(engine, **kw):
    from scripts.backfill_message_tombstones import find_orphan_created_ids

    async def go():
        with Session(engine) as s:
            return await find_orphan_created_ids(_Bridge(s), **kw)

    return _run(go())


def _apply(engine, orphans, execute, batch=500):
    from scripts.backfill_message_tombstones import apply_backfill

    async def go():
        with Session(engine) as s:
            # apply_backfill 每批自带提交点，外层不再 commit
            return await apply_backfill(_Bridge(s), orphans, execute=execute,
                                        batch=batch)

    return _run(go())


def _delete_sets(engine):
    from app.db.database import SyncEvent, SyncTombstone
    with Session(engine) as s:
        events = s.execute(select(SyncEvent).where(
            SyncEvent.entity_type == "messages",
            SyncEvent.op == "delete")).scalars().all()
        tombs = s.execute(select(SyncTombstone).where(
            SyncTombstone.entity_type == "messages")).scalars().all()
    return {e.entity_id for e in events}, {t.entity_id for t in tombs}, events


def test_find_orphan_created_ids_basic():
    engine = _env()
    _seed(engine)
    found = _find(engine)
    ids = {o["entity_id"] for o in found}
    assert ids == {"a1", "old1"}, f"只应命中孤儿，实得 {ids}"
    a1 = next(o for o in found if o["entity_id"] == "a1")
    assert a1["user_id"] == "owner" and a1["conversation_id"] == "c1"


def test_find_since_filter():
    engine = _env()
    _seed(engine)
    found = _find(engine, since=_BASE - timedelta(days=1))
    ids = {o["entity_id"] for o in found}
    assert ids == {"a1"}, f"--since 应过滤 30 天前事件，实得 {ids}"


def test_dry_run_writes_nothing():
    engine = _env()
    _seed(engine)
    found = _find(engine)
    out = _apply(engine, found, execute=False)
    assert out["written"] == 0
    ev_ids, tb_ids, _ = _delete_sets(engine)
    # t1 是 seed 存量墓碑；dry-run 不得新增任何事件/墓碑
    assert ev_ids == set() and tb_ids == {"t1"}


def test_execute_writes_event_and_tombstone():
    engine = _env()
    _seed(engine)
    found = _find(engine)
    out = _apply(engine, found, execute=True)
    assert out["written"] == 2, out
    ev_ids, tb_ids, events = _delete_sets(engine)
    assert ev_ids == {"a1", "old1"} and tb_ids == {"t1", "a1", "old1"}
    for e in events:
        assert e.payload is None, "delete 事件 payload 必须为 NULL"
        assert e.origin_device is None, "服务端补发 origin=NULL"
        assert e.user_id == "owner"


def test_idempotent_rerun():
    engine = _env()
    _seed(engine)
    found = _find(engine)
    _apply(engine, found, execute=True)
    again = _find(engine)
    assert again == [], f"补发后再查应为空，实得 {again}"
    out2 = _apply(engine, again, execute=True)
    assert out2["written"] == 0
    _, tb_ids, events = _delete_sets(engine)
    assert len([e for e in events if e.entity_id == "a1"]) == 1
    assert sorted(tb_ids) == ["a1", "old1", "t1"]


def test_apply_rechecks_revived_message():
    engine = _env()
    _seed(engine)
    found = _find(engine)
    # find 之后、apply 之前消息行复活（fail-safe 批内复查）
    from app.db.database import Message
    with Session(engine) as s:
        s.add(Message(id="a1", conversation_id="c1", role="assistant",
                      content="复活了", created_at=_BASE))
        s.commit()
    out = _apply(engine, found, execute=True)
    assert out["written"] == 1 and out["skipped_existing"] == 1, out
    ev_ids, tb_ids, _ = _delete_sets(engine)
    assert ev_ids == {"old1"} and tb_ids == {"t1", "old1"}


def test_batching_500():
    engine = _env()
    _seed(engine)
    # 追加 1200 条孤儿 create（c1 会话）
    from app.db.database import SyncEvent
    with Session(engine) as s:
        for i in range(1200):
            s.add(SyncEvent(user_id="owner", entity_type="messages",
                            entity_id=f"x{i}", op="create",
                            payload={"conversation_id": "c1"},
                            created_at=_BASE + timedelta(minutes=10 + i)))
        s.commit()
    found = _find(engine, batch=500)
    assert len(found) == 1202, len(found)
    out = _apply(engine, found, execute=True, batch=500)
    assert out["written"] == 1202 and out["batches"] == 3, out
    ev_ids, tb_ids, _ = _delete_sets(engine)
    assert len(ev_ids) == 1202 and len(tb_ids) == 1203  # +t1 存量墓碑
