"""BUG-B TDD — 编辑/重生成裁剪 purge 已删轮次的 subconscious 单元 + 检索会话缓存失效。

2026-10-09 用户报告：编辑/重发后新回答的思考与正文仍称「我已经 xxxx」。DB 切片
删除本身正确（test_edit_resend_trim.py 覆盖），泄漏面 = v2 记忆：
① subconscious_log 已摄入的「用户+助手各 500 字」原始单元不随消息删除失效
   （source_ids 记录来源消息 id，可同源 purge）；
② 检索会话缓存（5min TTL，key=最近 3 user query 哈希）在 regenerate 同 key
   时重放删除前的记忆上下文。

修复契约：
1. `_delete_message_slice(..., user_id=...)` 删除消息同时 purge 同用户
   subconscious_log 中 source_ids 引用任一被删消息 id 的行（JSON 文本
   LIKE '%"{id}"%'）；他用户行/无关行/file_memory 行不受影响；
2. user_id=None（旧调用形）行为不变——只删消息不 purge（向后兼容）；
3. 同用户检索会话缓存随切片删除失效；他用户缓存不受影响；
4. purge 不自带 commit（与切片删除同事务，回滚即完整恢复）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_trim_purges_memory.py -v
"""
import asyncio

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.api.chat import _trim_messages_for_edit, _trim_messages_for_regeneration
from app.db.database import Base, Conversation, Message, SubconsciousLog, User
from app.services.memory_retrieval_service import (
    _session_cache,
    _session_cache_get,
    _session_cache_put,
    _text_injected_ids,
    invalidate_session_cache_for_user,
)


class _Bridge:
    """asyncio 鸭子壳包真实 sync Session（SQLite 内存）——与 test_edit_resend_trim.py 同缝。"""

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
        self._s.add(obj)

    async def flush(self):
        self._s.flush()


def _fresh():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    # FK 传递闭包一次建全（messages→conversations→users；subconscious_log→users）
    tables, stack, seen = [], [
        Conversation.__table__, Message.__table__, SubconsciousLog.__table__,
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


def _seed(sess, conversation_id="c1"):
    """u1(user) → a1(assistant)；subconscious 五行覆盖全部保留/删除分支。"""
    sess.add_all([
        User(id="owner", username="u", password_hash="x"),
        User(id="other", username="o", password_hash="x"),
        Conversation(id=conversation_id, user_id="owner", title="t"),
        Message(id="u1", conversation_id=conversation_id, role="user", content="编辑我"),
        Message(id="a1", conversation_id=conversation_id, role="assistant", content="我已经读取了文件"),
    ])
    sess.commit()
    # subconscious 行单独落（与 test_edit_resend_trim 的 payload 行同因——单提交插入序纠缠）
    sess.add_all([
        SubconsciousLog(id="s_hit_pair", user_id="owner", unit_kind="message",
                        raw_text="用户: 编辑我\n助手: 我已经读取了文件",
                        source_ids='["u1", "a1"]'),
        SubconsciousLog(id="s_hit_assistant_only", user_id="owner", unit_kind="message",
                        raw_text="助手残稿", source_ids='["a1"]'),
        SubconsciousLog(id="s_keep_unrelated", user_id="owner", unit_kind="message",
                        raw_text="别的轮次", source_ids='["u0", "a0"]'),
        SubconsciousLog(id="s_keep_other_user", user_id="other", unit_kind="message",
                        raw_text="同 id 他用户", source_ids='["u1", "a1"]'),
        SubconsciousLog(id="s_keep_file_memory", user_id="owner", unit_kind="file_memory",
                        raw_text="文件记忆条目", source_ids='["file_memory:AGENT:deadbeef"]'),
    ])
    sess.commit()


def _subconscious_ids(sess):
    return set(sess.execute(select(SubconsciousLog.id)).scalars().all())


class TestTrimPurgeSubconscious:
    def test_trim_edit_purges_units_sourced_from_deleted_messages(self):
        """契约 1：编辑切片 purge 同用户命中行（成对+仅助手引用）；保留无关/他用户/file_memory。"""
        sess, db = _fresh()
        _seed(sess)
        asyncio.run(_trim_messages_for_edit(
            db, conversation_id="c1", edit_message_id="u1", user_id="owner"))
        sess.commit()
        assert set(sess.execute(select(Message.id)).scalars().all()) == set()
        assert _subconscious_ids(sess) == {"s_keep_unrelated", "s_keep_other_user", "s_keep_file_memory"}

    def test_trim_regeneration_purges_too(self):
        """regenerate 路径同契约：目标 assistant 及其后消息删除，命中单元 purge。"""
        sess, db = _fresh()
        _seed(sess)
        asyncio.run(_trim_messages_for_regeneration(
            db, conversation_id="c1", regenerate_from_message_id="a1", user_id="owner"))
        sess.commit()
        assert set(sess.execute(select(Message.id)).scalars().all()) == {"u1"}
        # a1 被删 → s_hit_pair/s_hit_assistant_only 命中 purge
        assert _subconscious_ids(sess) == {"s_keep_unrelated", "s_keep_other_user", "s_keep_file_memory"}

    def test_no_user_id_keeps_legacy_behavior(self):
        """契约 2：user_id 缺省（旧调用形）只删消息不 purge（向后兼容）。"""
        sess, db = _fresh()
        _seed(sess)
        asyncio.run(_trim_messages_for_edit(db, conversation_id="c1", edit_message_id="u1"))
        sess.commit()
        assert set(sess.execute(select(Message.id)).scalars().all()) == set()
        assert _subconscious_ids(sess) == {
            "s_hit_pair", "s_hit_assistant_only", "s_keep_unrelated",
            "s_keep_other_user", "s_keep_file_memory",
        }

    def test_purge_defers_commit_atomic_with_slice(self):
        """契约 4：purge 不自带 commit——回滚后消息与 subconscious 行都完整恢复。"""
        sess, db = _fresh()
        _seed(sess)
        asyncio.run(_trim_messages_for_edit(
            db, conversation_id="c1", edit_message_id="u1", user_id="owner"))
        sess.rollback()
        assert set(sess.execute(select(Message.id)).scalars().all()) == {"u1", "a1"}
        assert _subconscious_ids(sess) == {
            "s_hit_pair", "s_hit_assistant_only", "s_keep_unrelated",
            "s_keep_other_user", "s_keep_file_memory",
        }


class TestSessionCacheInvalidation:
    def setup_method(self):
        _session_cache.clear()
        _text_injected_ids.clear()

    def teardown_method(self):
        _session_cache.clear()
        _text_injected_ids.clear()

    def test_trim_invalidates_invoking_users_cache_only(self):
        """契约 3：trim(user_id=owner) 后 owner 缓存失效、other 缓存保留。"""
        _session_cache_put("owner:deadbeef", "ctx-owner")
        _session_cache_put("other:deadbeef", "ctx-other")
        sess, db = _fresh()
        _seed(sess)
        asyncio.run(_trim_messages_for_edit(
            db, conversation_id="c1", edit_message_id="u1", user_id="owner"))
        assert _session_cache_get("owner:deadbeef") is None
        assert _session_cache_get("other:deadbeef") is not None

    def test_invalidate_clears_cross_turn_dedup_anchor(self):
        """D2 跨轮去重锚一并失效（已删轮次注入 id 不压制后续合法召回）。"""
        _session_cache_put("owner:deadbeef", "ctx-owner")
        _text_injected_ids["owner"] = {"m1", "m2"}
        invalidate_session_cache_for_user("owner")
        assert _session_cache_get("owner:deadbeef") is None
        assert "owner" not in _text_injected_ids
