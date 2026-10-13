"""RED tests (A4.8) — W8 retention 端到端安全缺口（UPSTREAM_TODO_20261007 项 8a-8d，2026-10-08）。

  8a  零事件用户不再假阳性 + per-user 裁剪水位（prune 落水位/delta 查水位）
  8b  resync 墓碑回放（tombstones 返回 + memory 域门控 + 客户端次序契约）
  8c  resync 先取游标再拍快照（race 注入判别：快照期提交实体 seq>返回游标）
  8d  heal 严格信任谓词 + 字段集对齐（legacy tool_calls）· per-flush 缓存清除 ·
      resync 批量解析逐块并入 + 孤儿告警按批汇总
  8d' prune 推送侧保护（已绑定用户 seq>push_cursor 只留不删）

Run: cd backend && python -m pytest tests/test_sync_w8_followups.py -v
"""
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import BigInteger, create_engine, null, select  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


@compiles(JSONB, "sqlite")
def _w8_jsonb_as_json(element, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _w8_bigint_as_int(element, compiler, **kw):
    return "INTEGER"


def _run(coro):
    return asyncio.run(coro)


class _AsyncShim:
    """apply/解析器/prune 所需 AsyncSession 最小面。"""

    def __init__(self, s):
        self._s = s

    async def execute(self, stmt, *a, **k):
        return self._s.execute(stmt, *a, **k)

    async def get(self, model, ident):
        return self._s.get(model, ident)

    def add(self, obj):
        self._s.add(obj)

    async def flush(self):
        self._s.flush()

    async def delete(self, obj):
        self._s.delete(obj)

    async def commit(self):
        self._s.commit()

    async def rollback(self):
        self._s.rollback()

    @property
    def no_autoflush(self):
        return self._s.no_autoflush


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


def _payload_env():
    """SQLite in-memory + 六表 + 真外置 hook。返回 (engine, conv_id, user_id)。"""
    from app.db.database import (Base, Conversation, Message, MessagePayload,
                                 SyncEvent, SyncTombstone, User)
    from app.services.message_payload_service import install_message_payload_hook

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        User.__table__, Conversation.__table__, Message.__table__,
        MessagePayload.__table__, SyncEvent.__table__, SyncTombstone.__table__,
    ])
    with Session(engine) as s:
        u = User(username="w8", password_hash="x")
        s.add(u)
        s.flush()
        c = Conversation(user_id=u.id, title="t")
        s.add(c)
        s.commit()
        conv_id, user_id = c.id, u.id
    install_message_payload_hook()
    return engine, conv_id, user_id


def _stub(field="reasoning_content", ref="a" * 64, preview="attack"):
    return json.dumps({"_externalized": True, "field": field, "payload_ref": ref,
                       "size_bytes": 6, "size_chars": 6, "preview": preview},
                      ensure_ascii=False)


# ── 8a/8d'：prune 水位 + 推送侧保护（SQLite 真表） ─────────────────────────

def _prune_env():
    from app.db.database import (Base, SyncEvent, SyncPruneWatermark, SyncState, User)

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        User.__table__, SyncEvent.__table__, SyncState.__table__,
        SyncPruneWatermark.__table__,
    ])
    return engine


def test_prune_records_watermarks_and_protects_unpushed_events():
    from app.db.database import SyncEvent, SyncPruneWatermark, SyncState
    from app.services.sync_retention import prune_sync_history

    engine = _prune_env()
    old = datetime.utcnow() - timedelta(days=200)
    recent = datetime.utcnow()
    with Session(engine) as s:
        s.add(SyncState(user_id="ua", device_id="d1", push_cursor=100,
                        sync_enabled=True, server_url="https://c.example",
                        cloud_username="ua", cloud_password="p"))
        # D-w8-6 复审修正：解绑态（enabled=false/密码清空）仍视绑定——解绑可续推
        s.add(SyncState(user_id="uc", device_id="d3", push_cursor=50,
                        sync_enabled=False, server_url="https://c.example",
                        cloud_username="uc", cloud_password=None))
        rows = [
            SyncEvent(seq=10, user_id="ua", entity_type="conversations", entity_id="c1",
                      op="create", created_at=old),          # 已推送 → 删
            SyncEvent(seq=200, user_id="ua", entity_type="conversations", entity_id="c2",
                      op="create", created_at=old),          # 未推送 → 保护
            SyncEvent(seq=20, user_id="ub", entity_type="conversations", entity_id="c3",
                      op="create", created_at=old),          # 未绑定 → 删
            SyncEvent(seq=300, user_id="ua", entity_type="conversations", entity_id="c4",
                      op="create", created_at=recent),       # 未过期 → 不动
            SyncEvent(seq=40, user_id="uc", entity_type="conversations", entity_id="c6",
                      op="create", created_at=old),          # 解绑态已推送 → 删
            SyncEvent(seq=70, user_id="uc", entity_type="conversations", entity_id="c7",
                      op="create", created_at=old),          # 解绑态未推送 → 保护
            SyncEvent(seq=210, user_id="ua", entity_type="conversations", entity_id="c8",
                      op="create", created_at=old,
                      origin_device="rx:cloud"),             # rx: 尾行（_push 不发送）→ 删
        ]
        s.add_all(rows)
        s.flush()
        # created_at=NULL 遗留行：Core insert + null() 绕过列默认（龄未知=最老）
        s.execute(SyncEvent.__table__.insert().values(
            seq=160, user_id="ua", entity_type="conversations", entity_id="c5",
            op="create", payload=None, origin_device=None, created_at=null()))
        s.commit()
    with Session(engine) as s:
        out = _run(prune_sync_history(_AsyncShim(s), 90))
    assert out["deleted_events"] == 4, f"应删 4（ua:10+210 · ub:20 · uc:40），实得 {out}"
    assert out["protected_unpushed"] == 3, f"保护 3（ua:200+160 · uc:70），实得 {out}"
    with Session(engine) as s:
        remaining = sorted(r.seq for r in s.execute(select(SyncEvent)).scalars().all())
        assert remaining == [70, 160, 200, 300], f"剩余 {remaining}"
        marks = {w.user_id: w.max_deleted_seq
                 for w in s.execute(select(SyncPruneWatermark)).scalars().all()}
        assert marks == {"ua": 210, "ub": 20, "uc": 40}, f"水位 {marks}"


def test_prune_watermark_uses_max_deleted_seq():
    """水位=被删事件的最大 seq（min 实现会让游标介于 min/max 的客户端静默漏）。"""
    from app.db.database import SyncEvent, SyncPruneWatermark, SyncState
    from app.services.sync_retention import prune_sync_history

    engine = _prune_env()
    old = datetime.utcnow() - timedelta(days=200)
    with Session(engine) as s:
        s.add(SyncState(user_id="ua", device_id="d1", push_cursor=100,
                        sync_enabled=True, server_url="https://c.example",
                        cloud_username="ua", cloud_password="p"))
        s.add_all([
            SyncEvent(seq=10, user_id="ua", entity_type="conversations", entity_id="c1",
                      op="create", created_at=old),
            SyncEvent(seq=30, user_id="ua", entity_type="conversations", entity_id="c2",
                      op="create", created_at=old),
        ])
        s.commit()
    with Session(engine) as s:
        out = _run(prune_sync_history(_AsyncShim(s), 90))
    assert out["deleted_events"] == 2
    with Session(engine) as s:
        mark = s.get(SyncPruneWatermark, "ua")
        assert mark is not None and mark.max_deleted_seq == 30, \
            f"水位必须=被删最大 seq(30)，实得 {getattr(mark, 'max_deleted_seq', None)}"


def test_prune_preview_matches_execute():
    from app.db.database import SyncEvent, SyncState
    from app.services.sync_retention import prune_sync_history, prune_sync_preview

    engine = _prune_env()
    old = datetime.utcnow() - timedelta(days=200)
    with Session(engine) as s:
        s.add(SyncState(user_id="ua", device_id="d1", push_cursor=100,
                        sync_enabled=True, server_url="https://c.example",
                        cloud_username="ua", cloud_password="p"))
        s.add_all([
            SyncEvent(seq=10, user_id="ua", entity_type="conversations", entity_id="c1",
                      op="create", created_at=old),
            SyncEvent(seq=200, user_id="ua", entity_type="conversations", entity_id="c2",
                      op="create", created_at=old),
        ])
        s.commit()
    with Session(engine) as s:
        prev = _run(prune_sync_preview(_AsyncShim(s), 90))
    assert prev["candidates"] == 2 and prev["protected_unpushed"] == 1 \
        and prev["deletable"] == 1, f"预览 {prev}"
    with Session(engine) as s:
        out = _run(prune_sync_history(_AsyncShim(s), 90))
    assert out["deleted_events"] == prev["deletable"] == 1, \
        "dry-run 预览的 deletable 必须等于真实删除数（执行前可核对保护面）"


def test_prune_watermark_never_decreases():
    from app.db.database import SyncEvent, SyncPruneWatermark, SyncState
    from app.services.sync_retention import prune_sync_history

    engine = _prune_env()
    old = datetime.utcnow() - timedelta(days=200)
    with Session(engine) as s:
        s.add(SyncState(user_id="ua", device_id="d1", push_cursor=100,
                        sync_enabled=True, server_url="https://c.example",
                        cloud_username="ua", cloud_password="p"))
        s.add(SyncPruneWatermark(user_id="ua", max_deleted_seq=90,
                                 updated_at=datetime.utcnow()))
        s.add(SyncEvent(seq=30, user_id="ua", entity_type="conversations",
                        entity_id="c1", op="create", created_at=old))
        s.commit()
    with Session(engine) as s:
        out = _run(prune_sync_history(_AsyncShim(s), 90))
    assert out["deleted_events"] == 1
    with Session(engine) as s:
        w = s.get(SyncPruneWatermark, "ua")
        assert w.max_deleted_seq == 90, "水位只增不减（30<90 保持）"
    # 第二轮：更高的被删 seq 推进水位
    with Session(engine) as s:
        s.add(SyncEvent(seq=95, user_id="ua", entity_type="conversations",
                        entity_id="c2", op="create", created_at=old))
        s.commit()
    with Session(engine) as s:
        out2 = _run(prune_sync_history(_AsyncShim(s), 90))
    assert out2["watermark_users"] == 1
    with Session(engine) as s:
        assert s.get(SyncPruneWatermark, "ua").max_deleted_seq == 95


# ── 8b/8c：resync 墓碑 + 游标先取 ─────────────────────────────────────────

class _Res:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows if rows is not None else []
        self._scalar = scalar

    def scalars(self):
        rows = self._rows
        return SimpleNamespace(all=lambda: rows)

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar


class _ResyncFakeDB:
    """语句感知 fake（W8 复审 D-w8-5 加固模型）：

    - 游标查询带 user 过滤（per-user 游标）——无过滤的实现拿到 999 污染值（判别）；
    - 墓碑查询无 user 过滤时额外返回他用户墓碑（作用域判别）；
    - race=True：第一轮快照期“提交” seq=101 的实体（轮末游标推进 → 触发重试）；
      第二轮快照返回该实体行（已提交必可见）——返回游标应=101 且行在场。
    """

    def __init__(self, tombstones=None, race=False, watermark=0, advance_always=False):
        self.tombstones = tombstones or []
        self.race = race
        self.watermark = watermark
        self.advance_always = advance_always
        self.race_injected = False
        self.cursor_calls = 0
        self.statements: list = []

    async def execute(self, stmt):
        s = str(stmt)
        self.statements.append(s)
        if "max(sync_events" in s:
            self.cursor_calls += 1
            if "user_id" not in s:
                return _Res(scalar=999)  # 无 per-user 过滤：全局噪声污染面
            if self.advance_always:
                return _Res(scalar=100 + self.cursor_calls)  # 每查必推进（钉重试上限）
            return _Res(scalar=101 if self.race_injected else 100)
        if "sync_prune_watermarks" in s:
            return _Res(scalar=self.watermark)
        if "sync_tombstones" in s:
            rows = list(self.tombstones)
            if "user_id" not in s:
                rows.append(SimpleNamespace(entity_type="conversations",
                                            entity_id="OTHER-USER-TOMB",
                                            deleted_at=datetime(2026, 10, 1)))
            return _Res(rows=rows)
        # 域快照 SELECT
        if self.race and not self.race_injected:
            self.race_injected = True
        if (self.race and self.race_injected and self.cursor_calls >= 2
                and "FROM conversations" in s):
            from app.db.database import Conversation
            return _Res(rows=[Conversation(id="race-entity-1", user_id="u-w8-resync",
                                           title="race")])
        return _Res(rows=[])


def _resync_user():
    from app.db.database import User
    return User(id="u-w8-resync", username="w8-resync", password_hash="x")


def test_resync_returns_tombstones_scoped_and_memory_gated():
    from app.api import sync as sync_api
    from app.core.config import get_config

    tb_rows = [
        SimpleNamespace(entity_type="conversations", entity_id="c-del-1",
                        deleted_at=datetime(2026, 10, 1, 12, 0, 0)),
        SimpleNamespace(entity_type="memory_episodes", entity_id="m-del-1",
                        deleted_at=datetime(2026, 10, 1, 12, 0, 0)),
    ]
    with _sync_on():
        fake = _ResyncFakeDB(tombstones=tb_rows)
        r = _run(sync_api.resync(db=fake, current_user=_resync_user()))
    tombs = {t["entity_type"]: t for t in r.tombstones}
    assert tombs["conversations"]["entity_id"] == "c-del-1"
    assert tombs["conversations"]["deleted_at"].endswith("Z")
    assert all(t["entity_id"] != "OTHER-USER-TOMB" for t in r.tombstones), \
        "墓碑必须按用户作用域（fake 仅对无过滤实现注入他用户行）"
    # 次序（D-w8-5）：游标先取 → 墓碑先于快照（「新墓碑+旧快照行」并存不可能）
    cursor_idx = next(i for i, s in enumerate(fake.statements) if "max(sync_events" in s)
    tomb_idx = next(i for i, s in enumerate(fake.statements) if "sync_tombstones" in s)
    snap_idx = next(i for i, s in enumerate(fake.statements)
                    if "max(sync_events" not in s and "sync_tombstones" not in s)
    assert cursor_idx < tomb_idx < snap_idx, \
        f"次序必须=游标→墓碑→快照（实得 {cursor_idx}/{tomb_idx}/{snap_idx}）"
    # memory 关闭 → memory 墓碑过滤（域门控同源）
    cfg = get_config()
    old = cfg._config.get("sync", {}).get("memory_enabled")
    with _sync_on():
        cfg._config.setdefault("sync", {})["memory_enabled"] = False
        try:
            fake2 = _ResyncFakeDB(tombstones=tb_rows)
            r2 = _run(sync_api.resync(db=fake2, current_user=_resync_user()))
        finally:
            if old is None:
                cfg._config["sync"].pop("memory_enabled", None)
            else:
                cfg._config["sync"]["memory_enabled"] = old
    assert not any(t["entity_type"].startswith("memory_") for t in r2.tombstones), \
        "memory 域关闭时墓碑随之过滤"
    assert any(t["entity_type"] == "conversations" for t in r2.tombstones)


def test_resync_stabilizes_cursor_race_with_retry():
    """D-w8-5：窗口内提交的实体在轮末被探测到 → 重试；重试轮快照兜住该实体。

    判别：无重试实现得到 cursor=100 且快照无 race 行（两断言同时红）；无 per-user
    过滤的实现拿到 999 污染游标。"""
    from app.api import sync as sync_api

    with _sync_on():
        fake = _ResyncFakeDB(race=True)
        r = _run(sync_api.resync(db=fake, current_user=_resync_user()))
    assert r.cursor == 101, (
        f"重试轮游标必须=推进后的用户 max(101)，实得 {r.cursor}"
        "（999=实现漏了 per-user 过滤）")
    assert any(s.get("id") == "race-entity-1"
               for s in r.entities.get("conversations") or []), \
        "重试轮快照必须兜住窗口内已提交实体（不依赖 delta 补偿）"
    assert fake.cursor_calls >= 4, (
        f"必须检测推进并重试（起/末游标查询×2 轮 ≥4），实得 {fake.cursor_calls}")


def test_resync_cursor_floors_at_prune_watermark():
    """复审 Minor 收口：水位 > 存活事件 max 时游标不得低于水位（否则 delta 恒提示
    resync → 重同步死循环）。"""
    from app.api import sync as sync_api

    with _sync_on():
        fake = _ResyncFakeDB(watermark=150)
        r = _run(sync_api.resync(db=fake, current_user=_resync_user()))
    assert r.cursor == 150, f"游标必须 floor 到水位 150（实得 {r.cursor}）"


def test_resync_retry_cap_bounds_passes():
    """复审 Minor 收口：每查必推进的最坏情形下重试封顶 3 轮（6 次游标查询），
    返回末轮起始游标（其后的推进由 delta 补偿）。"""
    from app.api import sync as sync_api

    with _sync_on():
        fake = _ResyncFakeDB(advance_always=True)
        r = _run(sync_api.resync(db=fake, current_user=_resync_user()))
    assert fake.cursor_calls == 6, f"重试必须封顶 3 轮（6 次），实得 {fake.cursor_calls}"
    assert r.cursor == 105, f"返回末轮起始游标 105（其后推进由 delta），实得 {r.cursor}"


# ── 8d：heal 严格谓词 + 字段集 ────────────────────────────────────────────

def test_heal_refuses_legal_stub_shaped_content():
    """合法内容恰形如 `{"_externalized": true}` 的 ≤阈值字段不得被误判孤儿覆盖。"""
    from app.db.database import Message, User
    from app.services.message_payload_service import uninstall_message_payload_hook
    from app.services.sync_apply import apply_change

    engine, conv_id, user_id = _payload_env()
    legal = json.dumps({"_externalized": True, "note": "user data"})
    try:
        with Session(engine) as s:
            m = Message(conversation_id=conv_id, role="assistant", content="x",
                        reasoning_content=legal, delivery_status="final")
            s.add(m)
            s.commit()
            mid = m.id
        with Session(engine) as s:
            user = s.get(User, user_id)
            res = _run(apply_change(_AsyncShim(s), user, "messages", mid, "create",
                                    {"conversation_id": conv_id, "role": "assistant",
                                     "content": "x", "reasoning_content": "REAL NEW TEXT",
                                     "delivery_status": "final"}))
            assert res == "skipped", f"非后端签发桩形内容必须 SKIPPED，实得 {res}"
            assert s.get(Message, mid).reasoning_content == legal, \
                "合法内容不得被 heal 覆盖（append-only）"
    finally:
        uninstall_message_payload_hook()


def test_heal_repairs_legacy_tool_calls_orphan():
    """heal 字段集与解析面（_RESOLVE_FIELDS）对齐：legacy tool_calls 孤儿桩可治。"""
    from app.db.database import Message, User
    from app.services.message_payload_service import (compute_sha256,
                                                      uninstall_message_payload_hook)
    from app.services.sync_apply import apply_change

    engine, conv_id, user_id = _payload_env()
    full = '{"items": [{"x": "y"}]}'
    legacy = json.dumps({"_externalized": True, "field": "tool_calls",
                         "payload_ref": compute_sha256("old-content"),
                         "size_bytes": 11, "size_chars": 11, "preview": "old"})
    try:
        with Session(engine) as s:
            m = Message(conversation_id=conv_id, role="assistant", content="x",
                        tool_calls=legacy, delivery_status="final")
            s.add(m)
            s.commit()
            mid = m.id
        with Session(engine) as s:
            user = s.get(User, user_id)
            res = _run(apply_change(_AsyncShim(s), user, "messages", mid, "create",
                                    {"conversation_id": conv_id, "role": "assistant",
                                     "content": "x", "tool_calls": full,
                                     "delivery_status": "final"}))
            assert res == "applied", f"legacy tool_calls 孤儿必须治愈，实得 {res}"
            assert s.get(Message, mid).tool_calls == full
    finally:
        uninstall_message_payload_hook()


def test_heal_skips_loose_stub_incoming():
    """入站桩判定保持宽松：任何桩形入站=无全文可治 → SKIPPED、原孤儿保持。"""
    from app.db.database import Message, User
    from app.services.message_payload_service import (compute_sha256,
                                                      uninstall_message_payload_hook)
    from app.services.sync_apply import apply_change

    engine, conv_id, user_id = _payload_env()
    orphan = _stub(ref=compute_sha256("gone"))
    loose_incoming = json.dumps({"_externalized": True})
    try:
        with Session(engine) as s:
            m = Message(conversation_id=conv_id, role="assistant", content="x",
                        reasoning_content=orphan, delivery_status="final")
            s.add(m)
            s.commit()
            mid = m.id
        with Session(engine) as s:
            user = s.get(User, user_id)
            res = _run(apply_change(_AsyncShim(s), user, "messages", mid, "create",
                                    {"conversation_id": conv_id, "role": "assistant",
                                     "content": "x", "reasoning_content": loose_incoming,
                                     "delivery_status": "final"}))
            assert res == "skipped", f"桩形入站不可治 → SKIPPED，实得 {res}"
            assert s.get(Message, mid).reasoning_content == orphan, \
                "无全文入站不得改写原孤儿桩"
    finally:
        uninstall_message_payload_hook()


# ── 8d：per-flush 缓存清除 + 解析器内存/告警 ──────────────────────────────

def test_flush_payload_cache_cleared_after_flush():
    from app.db.database import Message
    from app.services.message_payload_service import (FLUSH_PAYLOAD_CACHE_KEY,
                                                      uninstall_message_payload_hook)

    engine, conv_id, _uid = _payload_env()
    big = "Z" * 40000
    try:
        with Session(engine) as s:
            m = Message(conversation_id=conv_id, role="assistant", content="x",
                        reasoning_content=big, delivery_status="final")
            s.add(m)
            s.commit()
            assert FLUSH_PAYLOAD_CACHE_KEY not in s.info, \
                "flush 结束后 per-flush 缓存必须清除（会话不滞留全文引用）"
    finally:
        uninstall_message_payload_hook()


def test_resync_orphan_warnings_summarized(caplog):
    import app.services.sync_capture as sc
    from app.services.message_payload_service import (compute_sha256,
                                                      uninstall_message_payload_hook)

    engine, _conv_id, _uid = _payload_env()
    snaps = [
        {"id": f"m-orphan-{i}", "reasoning_content": _stub(ref=compute_sha256(f"a{i}"))}
        for i in range(8)
    ]
    try:
        with Session(engine) as s:
            with caplog.at_level(logging.WARNING, logger="app.services.sync_capture"):
                n = _run(sc.resolve_payload_stubs_batch(_AsyncShim(s), snaps))
        assert n == 0 and all(sn["reasoning_content"].startswith("{") for sn in snaps)
        keeps = [r for r in caplog.records if "keeping stub" in r.message]
        assert len(keeps) == 1, f"8 孤儿必须汇总为 1 条告警（防大史刷屏），实得 {len(keeps)}"
        assert "8" in keeps[0].message
    finally:
        uninstall_message_payload_hook()
