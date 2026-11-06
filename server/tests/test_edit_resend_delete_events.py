"""RED tests (A4.8) — P0 删除切片补 messages/delete 事件 + 墓碑（UPSTREAM_TODO_20261031，2026-10-31）。

事故（conv d7ea65b8 / 消息 f697cf2d）：编辑重发/重新生成走 `_delete_message_slice` 的
Core 批量 DELETE，绕过 SQLAlchemy mapper 事件——sync_capture 收不到 after_delete，
云端零 delete 事件、零墓碑；客户端只收到过旧回答的 create，永远收不到删除，
resync 只做 upsert+墓碑不做差集删除 → 本地永久残留空回答。

修复契约（工单 §2）：
1. 编辑/重生成删除的每条消息在同一事务产生 `messages/delete` SyncEvent + SyncTombstone；
2. delete 事件 seq 早于随后插入的新 user 消息 create；
3. 删除+事件+新消息同事务——回滚全部消失（test_trim_edit_defers_commit_atomic_replace 不破坏）；
4. streaming 行不发事件/墓碑；sync_enabled=false 零事件；事件 user_id=会话 owner；
   conversation_id 过滤防脏 slice；重复 id 幂等；origin_device 沿用 ContextVar（web 编辑=NULL）；
   1200 条切片分块 500；SimpleNamespace 拷贝不抛 UnmappedInstanceError；
5. 会话 ORM cascade 删除继续发 message delete 事件（防 passive_deletes 回归）。

Run: cd backend && python -m pytest tests/test_edit_resend_delete_events.py -v
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

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
    """asyncio 鸭子壳包真实 sync Session——与 test_edit_resend_trim.py 同缝。"""

    def __init__(self, sync_sess):
        self._s = sync_sess

    async def execute(self, stmt, *a, **k):
        return self._s.execute(stmt, *a, **k)

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


def _env():
    """SQLite 内存 + FK 传递闭包（含 sync_events/sync_tombstones/subconscious_log）。"""
    from app.db.database import (
        Base, ChatSession, Conversation, ConversationGroup, Message, MessagePayload,
        SubconsciousLog, SyncEvent, SyncTombstone,
    )

    engine = create_engine("sqlite://", poolclass=StaticPool)

    @sa_event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

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


class _sync_on:
    def __enter__(self):
        import app.core.config as _cfg
        self._cfg = _cfg.get_config()
        self._old = self._cfg._config.get("sync", {}).get("enabled")
        self._cfg._config.setdefault("sync", {})["enabled"] = True
        return self

    def __exit__(self, *exc):
        if self._old is None:
            self._cfg._config["sync"].pop("enabled", None)
        else:
            self._cfg._config["sync"]["enabled"] = self._old
        return False


class _sync_off:
    def __enter__(self):
        import app.core.config as _cfg
        self._cfg = _cfg.get_config()
        self._old = self._cfg._config.get("sync", {}).get("enabled")
        self._cfg._config.setdefault("sync", {})["enabled"] = False
        return self

    def __exit__(self, *exc):
        if self._old is None:
            self._cfg._config["sync"].pop("enabled", None)
        else:
            self._cfg._config["sync"]["enabled"] = self._old
        return False


def _capture():
    """干净注册 mapper 监听（w9 范式）。返回模块供 finally unregister。"""
    import app.services.sync_capture as sc
    sc.unregister_sync_capture()
    sc.register_sync_capture()
    return sc


_BASE = datetime(2026, 10, 30, 12, 0, 0)


def _mk_new_user_msg():
    from app.db.database import Message
    return Message(id="nu1", conversation_id="c1", role="user", content="新问题",
                   created_at=_BASE + timedelta(days=1))


def _row_streaming(row_id, conv_id):
    from app.db.database import Message
    return Message(id=row_id, conversation_id=conv_id, role="assistant",
                   content="", delivery_status="streaming",
                   created_at=_BASE + timedelta(minutes=5))


def _msg_id():
    from app.db.database import Message
    return Message.id


def _seed(engine, conversation_id="c1", owner="owner", n=0):
    """c1: u1(user) a1(assistant, 外置 stub+payload) u2 a2 [+ n 条 m<i>]。"""
    from app.db.database import Conversation, Message, MessagePayload, User
    from app.services.message_payload_service import build_field_stub

    fat = "x" * 5000
    stub = build_field_stub("tool_results", fat)
    with Session(engine) as s:
        s.add_all([
            User(id=owner, username="o", password_hash="x"),
            Conversation(id=conversation_id, user_id=owner, title="t"),
            Message(id="u1", conversation_id=conversation_id, role="user",
                    content="编辑我", created_at=_BASE),
            Message(id="a1", conversation_id=conversation_id, role="assistant",
                    content="回答", tool_results=stub, created_at=_BASE + timedelta(minutes=1)),
            Message(id="u2", conversation_id=conversation_id, role="user",
                    content="追问", created_at=_BASE + timedelta(minutes=2)),
            Message(id="a2", conversation_id=conversation_id, role="assistant",
                    content="再答", created_at=_BASE + timedelta(minutes=3)),
        ])
        s.commit()
        s.add(MessagePayload(message_id="a1", field="tool_results", content=fat,
                             size_bytes=len(fat), sha256="deadbeef"))
        if n:
            for i in range(n):
                s.add(Message(id=f"m{i}", conversation_id=conversation_id,
                              role="user", content=f"q{i}",
                              created_at=_BASE + timedelta(minutes=10 + i)))
        s.commit()


def _delete_sets(s):
    from app.db.database import SyncEvent, SyncTombstone
    events = s.execute(select(SyncEvent).where(
        SyncEvent.entity_type == "messages")).scalars().all()
    dels = {e.entity_id: e for e in events if e.op == "delete"}
    creates = {e.entity_id: e for e in events if e.op == "create"}
    tbs = {t.entity_id for t in s.execute(select(SyncTombstone).where(
        SyncTombstone.entity_type == "messages")).scalars()}
    return dels, creates, tbs


# ── 1/2：edit 路径事件 + seq 顺序 + 新消息 create 同事务 ────────────────────

def test_edit_resend_emits_delete_events_for_slice():
    from app.api.chat import _trim_messages_for_edit

    engine = _env()
    _seed(engine)
    sc = _capture()
    try:
        with Session(engine) as s, _sync_on():
            db = _Bridge(s)
            _run(_trim_messages_for_edit(db, conversation_id="c1",
                                         edit_message_id="u1", user_id="owner"))
            s.add(_mk_new_user_msg())
            s.commit()
            dels, creates, tbs = _delete_sets(s)
            assert set(dels) == {"u1", "a1", "u2", "a2"}, \
                f"切片每条消息都必须有 delete 事件，实得 {set(dels)}"
            assert all(e.payload is None for e in dels.values()), \
                "messages delete 事件 payload 恒 NULL"
            assert tbs == {"u1", "a1", "u2", "a2"}, f"墓碑集合不符: {tbs}"
            assert "nu1" in creates, "新 user 消息 create 事件缺失"
            assert set(s.execute(select(_msg_id())).scalars()) == {"nu1"}
    finally:
        sc.unregister_sync_capture()


def test_delete_events_order_before_new_user_create():
    from app.api.chat import _trim_messages_for_edit

    engine = _env()
    _seed(engine)
    sc = _capture()
    try:
        with Session(engine) as s, _sync_on():
            db = _Bridge(s)
            _run(_trim_messages_for_edit(db, conversation_id="c1",
                                         edit_message_id="u1", user_id="owner"))
            s.add(_mk_new_user_msg())
            s.commit()
            dels, creates, _ = _delete_sets(s)
            dmax = max(e.seq for e in dels.values())
            cmin = creates["nu1"].seq
            assert dmax < cmin, \
                f"delete seq({dmax}) 必须早于新消息 create seq({cmin})——对端不得短暂见新旧并存"
    finally:
        sc.unregister_sync_capture()


def test_delete_events_atomic_with_new_user_message():
    """删除后模拟新消息插入失败 rollback：旧消息、事件、墓碑全部未落库。"""
    from app.api.chat import _trim_messages_for_edit

    engine = _env()
    _seed(engine)
    sc = _capture()
    try:
        with Session(engine) as s, _sync_on():
            db = _Bridge(s)
            _run(_trim_messages_for_edit(db, conversation_id="c1",
                                         edit_message_id="u1", user_id="owner"))
            s.rollback()  # 模拟调用方新消息插入失败
            left = set(s.execute(select(_msg_id())).scalars())
            assert left == {"u1", "a1", "u2", "a2"}, f"回滚后旧消息必须完整: {left}"
            dels, _c, tbs = _delete_sets(s)
            assert not dels, "回滚后不得残留 delete 事件"
            assert not tbs, "回滚后不得残留墓碑"
    finally:
        sc.unregister_sync_capture()


# ── 3：regenerate 路径 ────────────────────────────────────────────────────

def test_regenerate_emits_delete_event_for_old_assistant():
    from app.api.chat import _trim_messages_for_regeneration

    engine = _env()
    _seed(engine)
    sc = _capture()
    try:
        with Session(engine) as s, _sync_on():
            db = _Bridge(s)
            _run(_trim_messages_for_regeneration(
                db, conversation_id="c1", regenerate_from_message_id="a1"))
            s.commit()
            dels, _c, tbs = _delete_sets(s)
            assert set(dels) == {"a1", "u2", "a2"}, f"regenerate 切片事件缺失: {set(dels)}"
            assert tbs == {"a1", "u2", "a2"}, f"regenerate 墓碑缺失: {tbs}"
    finally:
        sc.unregister_sync_capture()


# ── 4：边界 ───────────────────────────────────────────────────────────────

def test_delete_message_slice_deletes_only_slice():
    """脏 slice 跨会话：conversation_id 过滤生效，不误删他会话行。"""
    from app.api.chat_persistence import _delete_message_slice
    from app.db.database import Conversation

    engine = _env()
    _seed(engine)
    with Session(engine) as s:
        s.add(Conversation(id="c2", user_id="owner", title="t2"))
        s.add(_mk_other_conv_msg())
        s.commit()
    sc = _capture()
    try:
        with Session(engine) as s, _sync_on():
            rows = [SimpleNamespace(id="u1", conversation_id="c1"),
                    SimpleNamespace(id="x1", conversation_id="c2")]
            _run(_delete_message_slice(_Bridge(s), rows, user_id=None))
            s.commit()
            left = set(s.execute(select(_msg_id())).scalars())
            assert "x1" in left, "他会话行不得被 c1 的 slice 误删"
            assert "u1" not in left
            dels, _c, tbs = _delete_sets(s)
            assert set(dels) == {"u1"} and tbs == {"u1"}
    finally:
        sc.unregister_sync_capture()


def _mk_other_conv_msg():
    from app.db.database import Message
    return Message(id="x1", conversation_id="c2", role="user", content="他会话",
                   created_at=_BASE)


def test_delete_event_user_id_is_owner():
    """事件 user_id 来自会话 owner，不从调用方参数/请求体推断。"""
    from app.api.chat_persistence import _delete_message_slice

    engine = _env()
    _seed(engine)
    sc = _capture()
    try:
        with Session(engine) as s, _sync_on():
            rows = [SimpleNamespace(id="u1", conversation_id="c1")]
            _run(_delete_message_slice(_Bridge(s), rows, user_id="intruder"))
            s.commit()
            dels, _c, tbs = _delete_sets(s)
            assert dels and all(e.user_id == "owner" for e in dels.values()), \
                f"事件必须归属会话 owner: {[e.user_id for e in dels.values()]}"
    finally:
        sc.unregister_sync_capture()


def test_externalized_payload_stub_slice_no_unmapped_error():
    """SimpleNamespace 外置桩拷贝（2026-10-04 500 根因）在新实现下仍不抛
    UnmappedInstanceError，且事件照常发射。"""
    from app.api.chat import _trim_messages_for_edit

    engine = _env()
    _seed(engine)
    sc = _capture()
    try:
        with Session(engine) as s, _sync_on():
            db = _Bridge(s)
            _run(_trim_messages_for_edit(db, conversation_id="c1",
                                         edit_message_id="u1", user_id="owner"))
            s.commit()
            dels, _c, tbs = _delete_sets(s)
            assert "a1" in dels, "stub 行（SimpleNamespace 拷贝）也必须发射 delete 事件"
    finally:
        sc.unregister_sync_capture()


def test_large_slice_chunks_500():
    """1200 条切片：无参数上限错误、事件数=墓碑数=1200。"""
    from app.api.chat_persistence import _delete_message_slice

    engine = _env()
    _seed(engine, n=1200)
    sc = _capture()
    try:
        with Session(engine) as s, _sync_on():
            rows = [SimpleNamespace(id=f"m{i}", conversation_id="c1") for i in range(1200)]
            _run(_delete_message_slice(_Bridge(s), rows, user_id="owner"))
            s.commit()
            dels, _c, tbs = _delete_sets(s)
            assert len(dels) == 1200, f"事件数应为 1200，实得 {len(dels)}"
            assert len(tbs) == 1200, f"墓碑数应为 1200，实得 {len(tbs)}"
    finally:
        sc.unregister_sync_capture()


def test_delete_event_not_emitted_for_streaming_row():
    """streaming 在途行从未发过 create——删除它不得发 delete 事件/留墓碑（_emit 守卫）。"""
    from app.api.chat_persistence import _delete_message_slice

    engine = _env()
    _seed(engine)
    with Session(engine) as s:
        s.add(_row_streaming("s1", "c1"))
        s.commit()
    sc = _capture()
    try:
        with Session(engine) as s, _sync_on():
            rows = [SimpleNamespace(id="u1", conversation_id="c1"),
                    SimpleNamespace(id="s1", conversation_id="c1")]
            _run(_delete_message_slice(_Bridge(s), rows, user_id=None))
            s.commit()
            dels, _c, tbs = _delete_sets(s)
            assert "u1" in dels
            assert "s1" not in dels, "streaming 行不得发 delete 事件"
            assert "s1" not in tbs, "streaming 行不得留墓碑"
            assert s.execute(select(_msg_id()).where(_msg_id() == "s1")).first() is None, \
                "streaming 行本身仍应被删除"
    finally:
        sc.unregister_sync_capture()


def test_sync_disabled_no_events():
    """[sync].enabled=false：删除照常、零事件零墓碑（已知同步关闭窗口）。"""
    from app.api.chat_persistence import _delete_message_slice

    engine = _env()
    _seed(engine)
    sc = _capture()
    try:
        with Session(engine) as s, _sync_off():
            rows = [SimpleNamespace(id="u1", conversation_id="c1"),
                    SimpleNamespace(id="a1", conversation_id="c1")]
            _run(_delete_message_slice(_Bridge(s), rows, user_id=None))
            s.commit()
            left = set(s.execute(select(_msg_id())).scalars())
            assert left == {"u2", "a2"}, f"删除应照常执行: {left}"
            dels, _c, tbs = _delete_sets(s)
            assert not dels and not tbs, "sync 关闭时不得写事件/墓碑"
    finally:
        sc.unregister_sync_capture()


def test_duplicate_ids_tombstone_upsert_no_error():
    """切片含重复 id：去重后单事件，墓碑 upsert 不报错。"""
    from app.api.chat_persistence import _delete_message_slice
    from app.db.database import SyncEvent

    engine = _env()
    _seed(engine)
    sc = _capture()
    try:
        with Session(engine) as s, _sync_on():
            rows = [SimpleNamespace(id="u1", conversation_id="c1"),
                    SimpleNamespace(id="u1", conversation_id="c1"),
                    SimpleNamespace(id="a1", conversation_id="c1")]
            _run(_delete_message_slice(_Bridge(s), rows, user_id=None))
            s.commit()
            dels, _c, tbs = _delete_sets(s)
            u_events = s.execute(select(SyncEvent).where(
                SyncEvent.entity_type == "messages",
                SyncEvent.entity_id == "u1", SyncEvent.op == "delete")).all()
            assert len(u_events) == 1, f"重复 id 应去重为单事件，实得 {len(u_events)}"
            assert tbs == {"u1", "a1"}
    finally:
        sc.unregister_sync_capture()


def test_delete_event_origin_device_null_for_chat_edit():
    """web/客户端直接 /chat/stream 编辑无 device 上下文：origin_device=NULL（预期非 bug）。"""
    from app.api.chat_persistence import _delete_message_slice

    engine = _env()
    _seed(engine)
    sc = _capture()
    try:
        with Session(engine) as s, _sync_on():
            rows = [SimpleNamespace(id="u1", conversation_id="c1")]
            _run(_delete_message_slice(_Bridge(s), rows, user_id=None))
            s.commit()
            dels, _c, _t = _delete_sets(s)
            assert dels and all(e.origin_device is None for e in dels.values())
    finally:
        sc.unregister_sync_capture()


def test_conversation_delete_still_emits_message_deletes():
    """会话 ORM cascade 删除继续逐条发 message delete 事件
    （防未来给 Conversation.messages 加 passive_deletes=True 再次吞事件）。"""
    from app.db.database import Conversation

    engine = _env()
    _seed(engine)
    sc = _capture()
    try:
        with Session(engine) as s, _sync_on():
            conv = s.get(Conversation, "c1")
            s.delete(conv)
            s.commit()
            dels, _c, tbs = _delete_sets(s)
            assert {"u1", "a1", "u2", "a2"} <= set(dels), \
                f"cascade 删除必须逐条发事件: {set(dels)}"
            assert {"u1", "a1", "u2", "a2"} <= tbs
    finally:
        sc.unregister_sync_capture()
