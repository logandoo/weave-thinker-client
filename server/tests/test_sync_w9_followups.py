"""RED tests (A4.8) — W9 parked 项 9/10（UPSTREAM_TODO_20261007，2026-10-08）。

  项9  user_model_providers 自然键墓碑载荷：_emit 写 payload（仅自然键类型）·
       resync 序列化携带（非空才有键）· apply_change 自然键删除兜底 · 迁移在位
  项10 skill_files owns 分支：owner delete APPLIED / 非 owner SKIPPED

Run: cd backend && python -m pytest tests/test_sync_w9_followups.py -v
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import BigInteger, create_engine, select  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


@compiles(JSONB, "sqlite")
def _w9_jsonb_as_json(element, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _w9_bigint_as_int(element, compiler, **kw):
    return "INTEGER"


def _run(coro):
    return asyncio.run(coro)


class _AsyncShim:
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


def _w9_env():
    """SQLite in-memory + 八表。返回 (engine, user_id, other_user_id)。"""
    from app.db.database import (Base, Conversation, Message, SkillFile, SyncEvent,
                                 SyncTombstone, User, UserAsrHotword,
                                 UserModelProvider, UserSkill)

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        User.__table__, Conversation.__table__, Message.__table__,
        UserModelProvider.__table__, UserAsrHotword.__table__,
        UserSkill.__table__, SkillFile.__table__,
        SyncEvent.__table__, SyncTombstone.__table__,
    ])
    with Session(engine) as s:
        u = User(username="w9", password_hash="x")
        other = User(username="w9-other", password_hash="x")
        s.add_all([u, other])
        s.commit()
        return engine, u.id, other.id


# ── 项9：墓碑载荷（自然键） ──────────────────────────────────────────────

def test_emit_writes_natural_key_tombstone_payload_only():
    """_emit 对 user_model_providers delete 写自然键载荷；普通 delete 保持 NULL。"""
    import app.services.sync_capture as sc
    from app.db.database import (SyncTombstone, UserAsrHotword, UserModelProvider)

    engine, uid, _ = _w9_env()
    sc.unregister_sync_capture()
    sc.register_sync_capture()
    try:
        with Session(engine) as s, _sync_on():
            p = UserModelProvider(user_id=uid, kind="llm", provider="w9prov")
            s.add(p)
            s.commit()
            pid = p.id
            h = UserAsrHotword(user_id=uid, text="w9hot")
            s.add(h)
            s.commit()
            hid = h.id
            s.delete(s.get(UserAsrHotword, hid))
            s.commit()
            s.delete(s.get(UserModelProvider, pid))
            s.commit()
        with Session(engine) as s:
            tb_prov = s.execute(
                select(SyncTombstone).where(
                    SyncTombstone.entity_type == "user_model_providers")
            ).scalars().all()
            tb_other = s.execute(
                select(SyncTombstone).where(
                    SyncTombstone.entity_type == "user_asr_hotwords")
            ).scalars().all()
        assert len(tb_prov) == 1 and tb_prov[0].entity_id == pid, tb_prov
        assert tb_prov[0].payload == {"kind": "llm", "provider": "w9prov"}, \
            f"墓碑必须携自然键载荷，实得 {tb_prov[0].payload!r}"
        assert tb_other and tb_other[0].payload is None, \
            "非自然键类型墓碑载荷必须保持 NULL"
    finally:
        sc.unregister_sync_capture()


def test_apply_delete_natural_key_fallback_via_payload():
    """apply_change delete 按 id 未中 + payload 自然键 → 删除归并行（项9 契约）。"""
    from app.db.database import User, UserModelProvider
    from app.services.sync_apply import apply_change

    engine, uid, _ = _w9_env()
    with Session(engine) as s, _sync_on():
        user = s.get(User, uid)
        row = UserModelProvider(user_id=uid, kind="llm", provider="nk-prov")
        s.add(row)
        s.commit()
        rid = row.id
        res = _run(apply_change(_AsyncShim(s), user, "user_model_providers",
                                str(uuid.uuid4()), "delete",
                                {"kind": "llm", "provider": "nk-prov"}))
        assert res == "applied", f"自然键兜底必须 APPLIED，实得 {res}"
        assert s.get(UserModelProvider, rid) is None, "归并行必须被删除"


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
    """最小 resync fake（游标/水位/墓碑/域快照）。"""

    def __init__(self, tombstones=None):
        self.tombstones = tombstones or []
        self.statements: list = []

    async def execute(self, stmt):
        s = str(stmt)
        self.statements.append(s)
        if "max(sync_events" in s:
            return _Res(scalar=0)
        if "sync_prune_watermarks" in s:
            return _Res(scalar=0)
        if "sync_tombstones" in s:
            return _Res(rows=self.tombstones)
        return _Res(rows=[])


def test_resync_tombstone_entry_carries_payload_when_present():
    """resync 墓碑条目在 payload 非空时携带（自然键供对端 apply 兜底）；空则无键。"""
    from app.api import sync as sync_api
    from app.db.database import User

    tb_rows = [
        SimpleNamespace(entity_type="user_model_providers", entity_id="p-srv",
                        deleted_at=datetime(2026, 10, 8, 1, 0, 0),
                        payload={"kind": "llm", "provider": "w9prov"}),
        SimpleNamespace(entity_type="conversations", entity_id="c-srv",
                        deleted_at=datetime(2026, 10, 8, 1, 0, 0), payload=None),
    ]
    with _sync_on():
        fake = _ResyncFakeDB(tombstones=tb_rows)
        r = _run(sync_api.resync(db=fake, current_user=User(
            id="u-w9-resync", username="w9-resync", password_hash="x")))
    entries = {e["entity_id"]: e for e in r.tombstones}
    assert entries["p-srv"].get("payload") == {"kind": "llm", "provider": "w9prov"}, \
        f"自然键墓碑必须携带 payload，实得 {entries['p-srv']}"
    assert "payload" not in entries["c-srv"], \
        "空载荷不得输出 payload 键（体积/契约稳定）"


def test_emit_redelete_heals_null_payload_tombstone():
    """复审 A1 收口：存量空载荷墓碑在同 key 再次删除时以新载荷治愈（coalesce）。"""
    import app.services.sync_capture as sc
    from sqlalchemy import update as _update

    from app.db.database import SyncTombstone, UserModelProvider

    engine, uid, _ = _w9_env()
    sc.unregister_sync_capture()
    sc.register_sync_capture()
    try:
        with Session(engine) as s, _sync_on():
            p = UserModelProvider(user_id=uid, kind="llm", provider="heal-prov")
            s.add(p)
            s.commit()
            pid = p.id
            s.delete(s.get(UserModelProvider, pid))
            s.commit()
        # 模拟存量墓碑：载荷清空（迁移前产生/回填未及）
        with Session(engine) as s:
            s.execute(_update(SyncTombstone)
                      .where(SyncTombstone.entity_id == pid)
                      .values(payload=None))
            s.commit()
            cur = s.execute(select(SyncTombstone).where(
                SyncTombstone.entity_id == pid)).scalar_one()
            assert cur.payload is None, "前置：载荷已清空"
        # 同 id 重建 → 再删 → 载荷治愈
        with Session(engine) as s, _sync_on():
            s.add(UserModelProvider(id=pid, user_id=uid, kind="llm",
                                    provider="heal-prov"))
            s.commit()
            s.delete(s.get(UserModelProvider, pid))
            s.commit()
        with Session(engine) as s:
            tb = s.execute(select(SyncTombstone).where(
                SyncTombstone.entity_id == pid)).scalar_one()
        assert tb.payload == {"kind": "llm", "provider": "heal-prov"}, \
            f"再次删除必须治愈空载荷（on_conflict coalesce），实得 {tb.payload!r}"
    finally:
        sc.unregister_sync_capture()


def test_startup_migration_entries_present_and_ordered():
    """项9 迁移在位且置于 pgvector 跳过窗之前（非 memory-v2 迁移）。

    复审 A2/A3 收口：断言回填 SQL 的判别子句（对象载荷守卫 + 确定性 max(seq)），
    mutation 掉任一守则本条必红。"""
    from app.db.migrations import STARTUP_MIGRATIONS

    names = [v for v, _ in STARTUP_MIGRATIONS]
    for v in ("sync_tombstones_payload", "sync_tombstones_payload_backfill"):
        assert v in names, f"缺启动迁移 {v}"
        assert names.index(v) < names.index("pgvector_extension"), \
            f"{v} 必须置于 pgvector_extension 之前（后缀纯度守卫）"
    stmt = dict(STARTUP_MIGRATIONS)["sync_tombstones_payload"]
    assert "ALTER TABLE sync_tombstones ADD COLUMN IF NOT EXISTS payload JSONB" in stmt
    bf = dict(STARTUP_MIGRATIONS)["sync_tombstones_payload_backfill"]
    assert "UPDATE sync_tombstones" in bf and "sync_events" in bf
    assert "jsonb_typeof(e.payload) = 'object'" in bf, \
        "回填必须排除 JSON null 伪载荷（裸 IS NOT NULL 会任意命中）"
    assert "max(e2.seq)" in bf, "回填必须确定性取最新 delete 事件（max(seq)）"
    assert "(t.payload IS NULL OR jsonb_typeof(t.payload) = 'null')" in bf, \
        "回填目标须覆盖 SQL NULL 与 JSON null 两态"


# ── 项10：skill_files owns 分支 ─────────────────────────────────────────

def test_owns_skill_files_owner_true_foreign_false():
    from app.db.database import SkillFile, User, UserSkill
    from app.services.sync_apply import owns

    engine, uid, other_id = _w9_env()
    with Session(engine) as s:
        us = UserSkill(user_id=uid, name="sk-w9", content="c")
        s.add(us)
        s.flush()
        f = SkillFile(skill_id=us.id, file_path="a.py", file_content="x",
                      file_type="script")
        s.add(f)
        s.commit()
        fid = f.id
        user = s.get(User, uid)
        other = s.get(User, other_id)
        assert _run(owns(_AsyncShim(s), user, "skill_files",
                         s.get(SkillFile, fid))) is True, "owner 归属必须为真"
        assert _run(owns(_AsyncShim(s), other, "skill_files",
                         s.get(SkillFile, fid))) is False, "非 owner 归属必须为假"


def test_skill_files_delete_owner_applied_foreign_skipped():
    from app.db.database import SkillFile, User, UserSkill
    from app.services.sync_apply import apply_change

    engine, uid, other_id = _w9_env()
    with Session(engine) as s, _sync_on():
        us = UserSkill(user_id=uid, name="sk-w9b", content="c")
        s.add(us)
        s.flush()
        f = SkillFile(skill_id=us.id, file_path="b.py", file_content="x",
                      file_type="script")
        s.add(f)
        s.commit()
        fid = f.id
        other = s.get(User, other_id)
        res = _run(apply_change(_AsyncShim(s), other, "skill_files", fid,
                                "delete", None))
        assert res == "skipped", "非 owner delete 必须 SKIPPED"
        assert s.get(SkillFile, fid) is not None, "非 owner 不得删行"
        user = s.get(User, uid)
        res2 = _run(apply_change(_AsyncShim(s), user, "skill_files", fid,
                                 "delete", None))
        assert res2 == "applied", f"owner delete 必须 APPLIED，实得 {res2}"
        assert s.get(SkillFile, fid) is None, "owner delete 必须落删"


def test_owns_skill_files_null_skill_id_fails_closed():
    """复审 B3：NULL skill_id 早退（不查库、不触发 SAWarning），fail-closed。"""
    import warnings

    from app.services.sync_apply import owns

    class _RaiseOnGet:
        async def get(self, *a, **k):
            raise AssertionError("db.get 不得被调用（NULL skill_id 必须早退）")

    user = SimpleNamespace(id="u-null")
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # SAWarning（if any）即失败
        assert _run(owns(_RaiseOnGet(), user, "skill_files",
                         SimpleNamespace(skill_id=None))) is False
        assert _run(owns(_RaiseOnGet(), user, "skill_files",
                         SimpleNamespace())) is False
