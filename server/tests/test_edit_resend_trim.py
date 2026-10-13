"""Task2 TDD — 编辑/重生成裁剪在 payload 拷贝下 500 + 删建非原子。

conv-open 波（2026-10-03 上 60，b3184e8d7）后 `_load_conversation_messages`
经 `resolve_message_fields` 对外置 stub 行返回 SimpleNamespace 拷贝；
`_trim_messages_for_edit` 对拷贝 `db.delete()` → UnmappedInstanceError →
HTTP 500（conv 4e159a79「编辑内容并发送→500→内容消失」机制）。
且旧实现 trim 自带 commit：删+建两段落库，其间失败=编辑点后消息真丢。

修复契约：
1. 删除按 id 批量 DELETE（不碰 resolve 拷贝），payload 行随 DB CASCADE 清理；
2. 删除不自带 commit——删+建由调用方单事务 commit（失败回滚旧消息不丢）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_edit_resend_trim.py -v
"""
import asyncio

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.api.chat import _trim_messages_for_edit, _trim_messages_for_regeneration
from app.db.database import Base, Conversation, ConversationGroup, Message, MessagePayload, User
from app.services.message_payload_service import build_field_stub


class _Bridge:
    """asyncio 鸭子壳包真实 sync Session（SQLite 内存）——ORM 映射/删除语义全真
    （UnmappedInstanceError 等由真实 Session 抛出；execute/commit/delete 可 await）。
    `_is_async_session` 经 `iscoroutinefunction(execute)` 判定为 async 路径。"""

    def __init__(self, sync_sess):
        self._s = sync_sess

    async def execute(self, stmt):
        return self._s.execute(stmt)

    async def commit(self):
        self._s.commit()

    async def rollback(self):
        self._s.rollback()

    async def delete(self, obj):
        self._s.delete(obj)

    def add(self, obj):
        # 与真实 AsyncSession.add 一致为同步方法（免未 await 的协程泄漏）。
        self._s.add(obj)

    async def flush(self):
        self._s.flush()


def _fresh():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    # FK 传递闭包一次建全（messages→conversations→conversation_groups/assistants→users），
    # 不逐表手列——防止依赖再上溯时逐个撞表。
    tables, stack, seen = [], [
        ConversationGroup.__table__, Conversation.__table__,
        Message.__table__, MessagePayload.__table__,
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
    sess = Session(engine)
    return sess, _Bridge(sess)


FAT = "x" * 5000


def _seed(sess, conversation_id="c1"):
    """u1(user) → a1(assistant, 外置 stub + payload 行)；编辑目标 u1，切片含 stub 行。"""
    stub = build_field_stub("tool_results", FAT)
    sess.add_all([
        User(id="owner", username="u", password_hash="x"),
        Conversation(id=conversation_id, user_id="owner", title="t"),
        Message(id="u1", conversation_id=conversation_id, role="user", content="编辑我"),
        Message(id="a1", conversation_id=conversation_id, role="assistant", content="回答",
                tool_results=stub),
    ])
    sess.commit()
    # payload 行单独落（无 ORM relationship 的纯 FK——分开提交免插入序纠缠）
    sess.add(MessagePayload(message_id="a1", field="tool_results", content=FAT,
                            size_bytes=len(FAT), sha256="deadbeef"))
    sess.commit()


class TestTrimEditWithStubRows:
    def test_trim_edit_handles_stub_rows(self):
        """编辑切片含 SimpleNamespace 拷贝时不得抛 UnmappedInstanceError（500 机制）。"""
        sess, db = _fresh()
        _seed(sess)
        asyncio.run(_trim_messages_for_edit(db, conversation_id="c1", edit_message_id="u1"))
        sess.commit()  # 单事务点由调用方（chat_stream 删+建）持有
        left = set(sess.execute(select(Message.id)).scalars().all())
        assert left == set()

    def test_trim_edit_defers_commit_atomic_replace(self):
        """删+建单事务：删除不得自带 commit——删后回滚（模拟建消息失败）旧消息不丢。"""
        sess, db = _fresh()
        _seed(sess)
        asyncio.run(_trim_messages_for_edit(db, conversation_id="c1", edit_message_id="u1"))
        sess.rollback()
        left = set(sess.execute(select(Message.id)).scalars().all())
        assert left == {"u1", "a1"}

    def test_trim_regeneration_handles_stub_rows(self):
        """regenerate 路径同修复：目标为 assistant 消息，切片含 stub 行不崩。"""
        sess, db = _fresh()
        _seed(sess)
        out = asyncio.run(_trim_messages_for_regeneration(
            db, conversation_id="c1", regenerate_from_message_id="a1"))
        sess.commit()
        left = set(sess.execute(select(Message.id)).scalars().all())
        assert left == {"u1"}
        assert [m.id for m in out] == ["u1"]

    def test_trim_regeneration_defers_commit(self):
        """regenerate 删除同样不自带 commit——回滚后旧消息不丢（与调用方
        sort_order 提交点同事务；docstring 的事务边界声明实证）。"""
        sess, db = _fresh()
        _seed(sess)
        asyncio.run(_trim_messages_for_regeneration(
            db, conversation_id="c1", regenerate_from_message_id="a1"))
        sess.rollback()
        left = set(sess.execute(select(Message.id)).scalars().all())
        assert left == {"u1", "a1"}


class TestTrimEditRegressions:
    def test_trim_edit_plain_rows_still_delete(self):
        """无 stub 行的普通编辑：目标及其后消息删除、之前消息保留（行为不回归）。"""
        sess, db = _fresh()
        sess.add_all([
            User(id="owner", username="u", password_hash="x"),
            Conversation(id="c1", user_id="owner", title="t"),
            Message(id="u0", conversation_id="c1", role="user", content="更早"),
            Message(id="a0", conversation_id="c1", role="assistant", content="早回答"),
            Message(id="u1", conversation_id="c1", role="user", content="编辑我"),
            Message(id="a1", conversation_id="c1", role="assistant", content="后回答"),
        ])
        sess.commit()
        out = asyncio.run(_trim_messages_for_edit(db, conversation_id="c1", edit_message_id="u1"))
        sess.commit()
        left = set(sess.execute(select(Message.id)).scalars().all())
        assert left == {"u0", "a0"}
        assert [m.id for m in out] == ["u0", "a0"]

    def test_trim_edit_rejects_missing_and_non_user(self):
        """404/400 契约不变：目标不存在 404；目标非 user 400。"""
        import pytest
        from fastapi import HTTPException

        sess, db = _fresh()
        _seed(sess)
        with pytest.raises(HTTPException) as e404:
            asyncio.run(_trim_messages_for_edit(db, conversation_id="c1", edit_message_id="nope"))
        assert e404.value.status_code == 404
        with pytest.raises(HTTPException) as e400:
            asyncio.run(_trim_messages_for_edit(db, conversation_id="c1", edit_message_id="a1"))
        assert e400.value.status_code == 400

    def test_payload_rows_cascade_on_delete(self):
        """批量 DELETE 后 payload 行随 messages FK ON DELETE CASCADE 清理（批量删不破坏级联）。"""
        sess, db = _fresh()
        _seed(sess)
        asyncio.run(_trim_messages_for_edit(db, conversation_id="c1", edit_message_id="u1"))
        sess.commit()
        payloads = sess.execute(select(MessagePayload)).scalars().all()
        assert payloads == []
