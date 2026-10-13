"""RED tests (A4.8) — W7 后续三项（UPSTREAM_TODO_20261007 项 6/7/8，2026-10-08）。

  项6.1 resync 序列化面解析：messages 快照外置桩→全文（批量 + sha 校验；孤儿保桩）
  项6.2 heal-on-create：入站非桩全文治疗不可解析孤儿桩（健康/桩入站/普通行仍 SKIPPED）
  项7   batch 应用零 identity_map 扫描（per-flush 缓存 O(1)）+ 无 session 上下文告警
  项8   保留服务 prune_sync_history + delta min_available_seq/resync_required

Run: cd backend && python -m pytest tests/test_sync_w7_followups.py -v
"""
import asyncio
import json
import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _w7_jsonb_as_json(element, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _w7_bigint_as_int(element, compiler, **kw):
    return "INTEGER"


def _run(coro):
    return asyncio.run(coro)


class _AsyncShim:
    """apply/解析器所需 AsyncSession 最小面。"""

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
        u = User(username="w7", password_hash="x")
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


# ── 项6.1：resync 序列化面批量解析 ─────────────────────────────────────────

def test_resolve_payload_stubs_batch_replaces_verified_stub():
    import app.services.sync_capture as sc
    from app.db.database import Message
    from app.services.message_payload_service import uninstall_message_payload_hook

    engine, conv_id, _uid = _payload_env()
    big = "R" * 40000
    try:
        with Session(engine) as s:
            m = Message(conversation_id=conv_id, role="assistant", content="hi",
                        reasoning_content=big, delivery_status="final")
            s.add(m)
            s.commit()
        with Session(engine) as s:
            snaps = [sc.row_snapshot(r) for r in s.execute(select(Message)).scalars().all()]
            from app.services.message_payload_service import is_externalized_stub
            assert is_externalized_stub(snaps[0]["reasoning_content"]), "前置：行内=桩"
            n = _run(sc.resolve_payload_stubs_batch(_AsyncShim(s), snaps))
            assert n == 1, f"应替换 1 字段，实得 {n}"
            assert snaps[0]["reasoning_content"] == big, "快照必须=全文（项6.1）"
    finally:
        uninstall_message_payload_hook()


def test_resolve_payload_stubs_batch_keeps_orphan_and_tampered(caplog):
    import app.services.sync_capture as sc
    from app.db.database import Message
    from app.services.message_payload_service import (compute_sha256,
                                                      uninstall_message_payload_hook)

    engine, conv_id, _uid = _payload_env()
    big = "T" * 40000
    try:
        # 腿1 孤儿：快照桩无 payload 行
        orphan = {"id": "m-orphan-1", "reasoning_content": _stub(ref=compute_sha256("attack"))}
        with Session(engine) as s:
            with caplog.at_level(logging.WARNING, logger="app.services.sync_capture"):
                n = _run(sc.resolve_payload_stubs_batch(_AsyncShim(s), [orphan]))
        assert n == 0 and orphan["reasoning_content"].startswith("{"), "孤儿必须保持桩"
        assert any("keeping stub" in r.message for r in caplog.records), "孤儿需 warning"
        caplog.clear()
        # 腿2 篡改：行内容与桩 ref 不符
        with Session(engine) as s:
            m = Message(conversation_id=conv_id, role="assistant", content="x",
                        reasoning_content=big, delivery_status="final")
            s.add(m)
            s.commit()
            mid = m.id
        with Session(engine) as s:
            rows = s.execute(select(Message)).scalars().all()
            snaps = [sc.row_snapshot(r) for r in rows]
            snaps[0]["reasoning_content"] = _stub(ref=compute_sha256("attack"))
            with caplog.at_level(logging.WARNING, logger="app.services.sync_capture"):
                n = _run(sc.resolve_payload_stubs_batch(_AsyncShim(s), snaps))
        assert n == 0, "完整性不符不得替换"
        assert any("keeping stub" in r.message for r in caplog.records), "篡改需 warning"
    finally:
        uninstall_message_payload_hook()


def test_resync_endpoint_wired_to_batch_resolver():
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "app", "api", "sync.py"),
        encoding="utf-8").read()
    fn = src[src.index("async def resync("):]
    assert "resolve_payload_stubs_batch" in fn, "resync 必须调用批量解析（项6.1 接线钉）"


def test_batch_resolver_chunks_large_id_sets(monkeypatch):
    """复审 Important：ids 分块查询（asyncpg 单语句 32767 参数上限——超过即 500）。"""
    import app.services.sync_capture as sc
    from app.db.database import Message
    from app.services.message_payload_service import uninstall_message_payload_hook

    engine, conv_id, _uid = _payload_env()
    big = "C" * 40000
    monkeypatch.setattr(sc, "_PAYLOAD_LOOKUP_CHUNK", 3)
    try:
        with Session(engine) as s:
            for i in range(7):
                s.add(Message(conversation_id=conv_id, role="assistant", content=f"c{i}",
                              reasoning_content=big, delivery_status="final"))
            s.commit()

        class _CountingShim(_AsyncShim):
            def __init__(self, s):
                super().__init__(s)
                self.calls = 0

            async def execute(self, stmt, *a, **k):
                self.calls += 1
                return await super().execute(stmt, *a, **k)

        with Session(engine) as s:
            snaps = [sc.row_snapshot(r) for r in s.execute(select(Message)).scalars().all()]
            shim = _CountingShim(s)
            n = _run(sc.resolve_payload_stubs_batch(shim, snaps))
        assert n == 7 and all(sn["reasoning_content"] == big for sn in snaps)
        assert shim.calls == 3, f"7 ids/块3 应 3 次查询，实得 {shim.calls}"
    finally:
        uninstall_message_payload_hook()


def test_batch_resolver_resolves_legacy_tool_calls_stub():
    """复审 Minor：legacy tool_calls 桩（I5 前外置的历史行）在 resync 面同样解析
    （`_RESOLVE_FIELDS` 读侧超集，与展开路由一致）。"""
    import app.services.sync_capture as sc
    from app.db.database import Message, MessagePayload
    from app.services.message_payload_service import (compute_sha256,
                                                      uninstall_message_payload_hook)

    engine, conv_id, _uid = _payload_env()
    full = '{"items": [{"x": "y"}]}'
    try:
        with Session(engine) as s:
            stub = json.dumps({"_externalized": True, "field": "tool_calls",
                               "payload_ref": compute_sha256(full),
                               "size_bytes": len(full), "size_chars": len(full),
                               "preview": full[:10]})
            m = Message(id="legacy-mc-1", conversation_id=conv_id, role="assistant",
                        content="x", tool_calls=stub, delivery_status="final")
            s.add(m)
            s.flush()
            s.add(MessagePayload(message_id="legacy-mc-1", field="tool_calls",
                                 content=full, size_bytes=len(full),
                                 sha256=compute_sha256(full)))
            s.commit()
        with Session(engine) as s:
            snaps = [sc.row_snapshot(r) for r in s.execute(select(Message)).scalars().all()]
            n = _run(sc.resolve_payload_stubs_batch(_AsyncShim(s), snaps))
        assert n == 1 and snaps[0]["tool_calls"] == full, "legacy tool_calls 桩必须解析"
    finally:
        uninstall_message_payload_hook()


# ── 项6.2：heal-on-create ─────────────────────────────────────────────────

def test_heal_on_create_repairs_orphan_stub_and_propagates():
    import app.services.sync_capture as sc
    from app.db.database import Message, SyncEvent, User
    from app.services.message_payload_service import (compute_sha256, _lookup_payload,
                                                      stub_markers_trusted,
                                                      uninstall_message_payload_hook)
    from app.services.sync_apply import apply_change

    engine, conv_id, user_id = _payload_env()
    forged = _stub(ref=compute_sha256("attack"))
    big = "H" * 40000
    sc.unregister_sync_capture()
    sc.register_sync_capture()
    try:
        with Session(engine) as s:
            m = Message(conversation_id=conv_id, role="assistant", content="x",
                        reasoning_content=forged, delivery_status="final")
            s.add(m)
            s.commit()
            mid = m.id
        with Session(engine) as s, _sync_on():
            user = s.get(User, user_id)
            res = _run(apply_change(_AsyncShim(s), user, "messages", mid, "create",
                                    {"conversation_id": conv_id, "role": "assistant",
                                     "content": "x", "reasoning_content": big,
                                     "delivery_status": "final"}))
            assert res == "applied", f"heal 必须 APPLIED，实得 {res}"
            s.commit()
            row = s.get(Message, mid)
            assert stub_markers_trusted(row.reasoning_content), "治愈后应重外置为本地签发桩"
            assert _lookup_payload(s, mid, "reasoning_content") == big, "展开面必须可取回全文"
            events = [e for e in s.execute(select(SyncEvent)).scalars().all()
                      if e.entity_type == "messages" and e.entity_id == mid]
            assert events, "治愈必须发射同步事件（传播）"
            assert (events[-1].payload or {}).get("reasoning_content") == big, "事件须携全文"
    finally:
        sc.unregister_sync_capture()
        uninstall_message_payload_hook()


def test_heal_on_create_skips_healthy_stub_incoming_and_plain():
    from app.db.database import Message
    from app.services.message_payload_service import (_lookup_payload,
                                                      uninstall_message_payload_hook)
    from app.services.sync_apply import apply_change

    engine, conv_id, user_id = _payload_env()
    big = "K" * 40000
    from app.db.database import User
    try:
        # 腿1 健康：payload 行在场且 sha 相符 → 不得改写
        with Session(engine) as s:
            m = Message(conversation_id=conv_id, role="assistant", content="x",
                        reasoning_content=big, delivery_status="final")
            s.add(m)
            s.commit()
            mid = m.id
        with Session(engine) as s:
            user = s.get(User, user_id)
            res = _run(apply_change(_AsyncShim(s), user, "messages", mid, "create",
                                    {"conversation_id": conv_id, "role": "assistant",
                                     "content": "x", "reasoning_content": "EVIL" * 9999,
                                     "delivery_status": "final"}))
            assert res == "skipped", f"健康行必须 SKIPPED（append-only），实得 {res}"
            assert _lookup_payload(s, mid, "reasoning_content") == big, "健康内容不得被覆盖"
        # 腿2 桩入站：孤儿行 + 入站桩 → 不可治 → SKIPPED、保持原桩
        forged = _stub()
        with Session(engine) as s:
            m2 = Message(conversation_id=conv_id, role="assistant", content="y",
                         reasoning_content=forged, delivery_status="final")
            s.add(m2)
            s.commit()
            mid2 = m2.id
        with Session(engine) as s:
            user = s.get(User, user_id)
            res = _run(apply_change(_AsyncShim(s), user, "messages", mid2, "create",
                                    {"conversation_id": conv_id, "role": "assistant",
                                     "content": "y", "reasoning_content": _stub(ref="b" * 64),
                                     "delivery_status": "final"}))
            assert res == "skipped", "桩入站不可治 → SKIPPED"
            assert s.get(Message, mid2).reasoning_content == forged, "原桩不得被替换"
        # 腿3 普通行（非桩内联）：无孤儿可言 → SKIPPED
        with Session(engine) as s:
            m3 = Message(conversation_id=conv_id, role="assistant", content="z",
                         reasoning_content="short", delivery_status="final")
            s.add(m3)
            s.commit()
            mid3 = m3.id
        with Session(engine) as s:
            user = s.get(User, user_id)
            res = _run(apply_change(_AsyncShim(s), user, "messages", mid3, "create",
                                    {"conversation_id": conv_id, "role": "assistant",
                                     "content": "z", "reasoning_content": "NEWTEXT",
                                     "delivery_status": "final"}))
            assert res == "skipped"
            assert s.get(Message, mid3).reasoning_content == "short", "普通行不得被改写"
    finally:
        uninstall_message_payload_hook()


# ── 项7：per-flush 缓存（批量应用零候选扫描） ─────────────────────────────

def test_heal_cross_user_denied():
    """复审 Critical 回归：heal 写路径必须先过行归属——跨用户 push 不得改写
    他人消息（不依赖 UUID 保密）。"""
    from app.db.database import Conversation, Message, User
    from app.services.message_payload_service import uninstall_message_payload_hook
    from app.services.sync_apply import apply_change

    engine, conv_a, user_a = _payload_env()
    forged = _stub(ref="c" * 64)
    big = "X" * 40000
    try:
        with Session(engine) as s:
            m = Message(conversation_id=conv_a, role="assistant", content="x",
                        reasoning_content=forged, delivery_status="final")
            s.add(m)
            s.commit()
            victim_msg = m.id
        with Session(engine) as s:
            ub = User(username="attacker", password_hash="x")
            s.add(ub)
            s.flush()
            cb = Conversation(user_id=ub.id, title="b")
            s.add(cb)
            s.commit()
            attacker_id, attack_conv = ub.id, cb.id
        with Session(engine) as s:
            attacker = s.get(User, attacker_id)
            res = _run(apply_change(_AsyncShim(s), attacker, "messages", victim_msg,
                                    "create",
                                    {"conversation_id": attack_conv, "role": "assistant",
                                     "content": "x", "reasoning_content": big,
                                     "delivery_status": "final"}))
            assert res == "skipped", f"跨用户 heal 必须 SKIPPED，实得 {res}"
            assert s.get(Message, victim_msg).reasoning_content == forged, \
                "受害者字段不得被改写（归属校验缺失=Critical）"
    finally:
        uninstall_message_payload_hook()


def test_heal_two_fields_single_flush_event():
    """复审 Minor 收口：双字段孤儿一次治愈，不得中途 autoflush 出半治事件。"""
    import app.services.sync_capture as sc
    from app.db.database import Message, SyncEvent, User
    from app.services.message_payload_service import (_lookup_payload,
                                                      compute_sha256,
                                                      uninstall_message_payload_hook)
    from app.services.sync_apply import apply_change

    engine, conv_id, user_id = _payload_env()
    forged_rc = _stub(ref=compute_sha256("a1"))
    forged_tr = _stub(field="tool_results", ref=compute_sha256("a2"))
    big_rc = "R" * 40000
    big_tr = json.dumps({"results": [{"content": "T" * 40000}]})
    sc.unregister_sync_capture()
    sc.register_sync_capture()
    try:
        with Session(engine) as s:
            m = Message(conversation_id=conv_id, role="assistant", content="x",
                        reasoning_content=forged_rc, tool_results=forged_tr,
                        delivery_status="final")
            s.add(m)
            s.commit()
            mid = m.id
            before = len([e for e in s.execute(select(SyncEvent)).scalars().all()
                          if e.entity_type == "messages" and e.entity_id == mid])
        with Session(engine) as s, _sync_on():
            user = s.get(User, user_id)
            res = _run(apply_change(_AsyncShim(s), user, "messages", mid, "create",
                                    {"conversation_id": conv_id, "role": "assistant",
                                     "content": "x", "reasoning_content": big_rc,
                                     "tool_results": big_tr, "delivery_status": "final"}))
            assert res == "applied"
            s.commit()
            evs = [e for e in s.execute(select(SyncEvent)).scalars().all()
                   if e.entity_type == "messages" and e.entity_id == mid]
            assert len(evs) - before == 1, (
                f"双字段治愈必须新增单事件（no_autoflush），实得 {len(evs) - before}")
            assert (evs[-1].payload or {}).get("reasoning_content") == big_rc
            assert (evs[-1].payload or {}).get("tool_results") == big_tr
            assert _lookup_payload(s, mid, "reasoning_content") == big_rc
            assert _lookup_payload(s, mid, "tool_results") == big_tr
    finally:
        sc.unregister_sync_capture()
        uninstall_message_payload_hook()


def test_batch_apply_zero_session_candidate_scans(monkeypatch):
    import app.services.sync_capture as sc
    from app.db.database import Message, SyncEvent
    from app.services.message_payload_service import uninstall_message_payload_hook

    engine, conv_id, _uid = _payload_env()
    real = getattr(sc, "_session_payload_candidates", None)
    assert real is not None, "sync_capture 缺 _session_payload_candidates（项7 候选扫描未抽缝）"
    calls = {"n": 0}

    def spy(sess):
        calls["n"] += 1
        return real(sess)

    monkeypatch.setattr(sc, "_session_payload_candidates", spy)
    sc.unregister_sync_capture()
    sc.register_sync_capture()
    big = "B" * 40000
    try:
        with Session(engine) as s, _sync_on():
            for i in range(30):
                m = Message(conversation_id=conv_id, role="assistant", content=f"m{i}",
                            reasoning_content=big, delivery_status="final")
                s.add(m)
                s.flush()  # 每消息一 flush：模拟 resync 批量应用的单 session 节奏
            s.commit()
            events = [e for e in s.execute(select(SyncEvent)).scalars().all()
                      if e.entity_type == "messages"]
            assert len(events) == 30
            assert all((e.payload or {}).get("reasoning_content") == big for e in events), \
                "批量应用事件载荷均须全文（正确性不退）"
            assert calls["n"] == 0, (
                f"缓存命中路径不得触发 identity_map 候选扫描（实得 {calls['n']} 次；"
                "RED 基线=每消息一次 O(N) 扫描）")
    finally:
        sc.unregister_sync_capture()
        uninstall_message_payload_hook()


def test_resolver_warns_without_session_context(caplog):
    import app.services.sync_capture as sc
    from app.db.database import Message

    forged = _stub()
    detached = Message(id="detached-w7", conversation_id="c-x", role="assistant",
                       content="x", reasoning_content=forged, delivery_status="final")
    payload = {"reasoning_content": forged}

    class _Conn:
        def execute(self, stmt, *a, **k):
            return SimpleNamespace(first=lambda: None)

    with caplog.at_level(logging.WARNING, logger="app.services.sync_capture"):
        sc._resolve_payload_fields(_Conn(), detached, payload)
    assert payload["reasoning_content"] == forged, "无上下文时不得外发未校验内容"
    assert any("no session context" in r.message for r in caplog.records), \
        "object_session=None 必须告警而非静默退回（项7 附带）"


# ── 项8：保留服务 + delta 缺口信号 ────────────────────────────────────────

def test_prune_sync_history_service():
    from app.services.sync_retention import prune_sync_history

    stmts = []

    class _Res:
        def __init__(self, rc=0):
            self.rowcount = rc

        def scalars(self):
            return SimpleNamespace(all=lambda: [])

        def all(self):
            return []

    class _FakeDB:
        def __init__(self):
            self.committed = False

        async def execute(self, stmt):
            stmts.append(str(stmt))
            return _Res(5) if str(stmt).lstrip().upper().startswith("DELETE") else _Res()

        async def commit(self):
            self.committed = True

    db = _FakeDB()
    out = _run(prune_sync_history(db, 90))
    assert out["deleted_events"] == 5 and out["cutoff"], out
    deletes = [s for s in stmts if s.lstrip().upper().startswith("DELETE")]
    assert len(deletes) == 1 and "sync_events" in deletes[0], f"仅裁剪事件表：{stmts}"
    assert "sync_tombstones" not in deletes[0], "墓碑不随龄裁剪（复审裁定 D-w4）"
    assert "is null" in deletes[0].lower(), "NULL 遗留行须计入裁剪（防永久泄漏）"
    assert db.committed

    out2 = _run(prune_sync_history(_FakeDB(), 0))
    assert out2.get("skipped"), "days<=0 必须为 no-op"


def test_delta_reports_gap_signal():
    from app.api import sync as sync_api

    user = SimpleNamespace(id="u1", username="u1")

    class _EventsRes:
        def scalars(self):
            return SimpleNamespace(all=lambda: [])

    class _MinRes:
        def __init__(self, v):
            self._v = v

        def scalar(self):
            return self._v

    class _FakeDB:
        """语句感知 fake：min_seq 查询 vs per-user 水位查询分别应答。"""

        def __init__(self, minv, watermark=None):
            self.minv = minv
            self.watermark = watermark
            self.calls = 0

        async def execute(self, stmt):
            self.calls += 1
            s = str(stmt)
            if "sync_prune_watermarks" in s:
                return _MinRes(self.watermark)
            if "min(sync_events" in s:
                return _MinRes(self.minv)
            return _EventsRes()

    # 游标越过保留沿 → 必须提示全量重同步
    r = _run(sync_api.get_delta(cursor=10, limit=200, device_id=None,
                                db=_FakeDB(50), current_user=user))
    assert getattr(r, "min_available_seq", None) == 50, "delta 必须携带保留沿（项8）"
    assert r.resync_required is True, "游标越过保留沿必须 resync_required"
    # 游标在保留窗内 → 正常增量
    r2 = _run(sync_api.get_delta(cursor=60, limit=200, device_id=None,
                                 db=_FakeDB(50), current_user=user))
    assert r2.resync_required is False
    # 零事件用户（resync 后 cursor>0、日志空、无水位）→ 不再假阳性
    # （W8 8a 迁移：旧行为恒真致 resync 风暴；行为修正经 D-w8-1 裁定）
    r3 = _run(sync_api.get_delta(cursor=60, limit=200, device_id=None,
                                 db=_FakeDB(None, None), current_user=user))
    assert r3.resync_required is False, "零事件用户不得恒判 resync（风暴修复）"
    # 日志被全部裁剪（无存活事件但有水位）→ 必须 resync
    r4 = _run(sync_api.get_delta(cursor=60, limit=200, device_id=None,
                                 db=_FakeDB(None, 70), current_user=user))
    assert r4.resync_required is True, "游标低于裁剪水位必须 resync_required"
    # 水位 ≤ cursor → 无缺口
    r5 = _run(sync_api.get_delta(cursor=60, limit=200, device_id=None,
                                 db=_FakeDB(None, 50), current_user=user))
    assert r5.resync_required is False
    # cursor=0 边界维持既有语义（新设备先 resync）
    r6 = _run(sync_api.get_delta(cursor=0, limit=200, device_id=None,
                                 db=_FakeDB(None, 70), current_user=user))
    assert r6.resync_required is False
    # 等值边界（A4.9 复审 A Minor 收口）：cursor==min_seq-1 / cursor==watermark → 无缺口
    r7 = _run(sync_api.get_delta(cursor=50, limit=200, device_id=None,
                                 db=_FakeDB(51, None), current_user=user))
    assert r7.resync_required is False, "cursor==min_seq-1 不得误报"
    r8 = _run(sync_api.get_delta(cursor=50, limit=200, device_id=None,
                                 db=_FakeDB(None, 50), current_user=user))
    assert r8.resync_required is False, "cursor==watermark 不得误报"
