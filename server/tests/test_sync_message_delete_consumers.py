"""下游 L2（2026-11-05）：远端 messages delete 落地后的本地清理契约。

背景：UPSTREAM_TODO_20261031 §5.3。客户端 `_apply_remote` 收到远端 delete 时，
除了删除本地行，还必须：
  1. 清本地待推队列（同 id 的 create/update）——防 push 重试把已删消息复活；
  2. messages：按 message id purge 本地 SubconsciousLog、失效检索缓存、
     失效该会话的 rolling summary/resume 快照（对齐服务端 _delete_message_slice）。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_sync_message_delete_consumers.py -v
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.sync_client as sc


class _FakeDB:
    def __init__(self):
        self.executed = []
        self.get_calls = []
        self.commits = 0

    async def get(self, model, ident):
        self.get_calls.append((model, ident))
        return SimpleNamespace(id=ident, conversation_id="c1")

    async def execute(self, stmt):
        self.executed.append(stmt)
        return None

    def begin_nested(self):
        class _N:
            async def __aenter__(self_inner):
                return self

            async def __aexit__(self_inner, *a):
                return False
        return _N()

    async def commit(self):
        self.commits += 1

    def add(self, obj):
        pass


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _user():
    return SimpleNamespace(id="u1", username="u")


def _ev(op="delete"):
    return {
        "entity_type": "messages", "entity_id": "m1", "op": op,
        "payload": None, "origin_device": "dev-b",
    }


def test_messages_delete_clears_queue_and_local_derivatives(monkeypatch):
    c = sc.SyncClient()
    db = _FakeDB()
    cleaned = AsyncMock()
    monkeypatch.setattr(c, "_clear_queued_events", cleaned)
    monkeypatch.setattr(sc, "apply_change", AsyncMock(return_value=sc.APPLIED))
    cache_invalidated = []
    monkeypatch.setattr(
        "app.services.memory_retrieval_service.invalidate_session_cache_for_user",
        lambda uid: cache_invalidated.append(uid),
    )
    continuity = AsyncMock()
    monkeypatch.setattr(
        "app.services.context_continuity_service.invalidate_continuity", continuity
    )

    asyncio.run(c._apply_remote(db, _user(), None, _ev("delete")))

    cleaned.assert_awaited_once()
    assert cleaned.await_args.args[2]["entity_id"] == "m1"
    assert cache_invalidated == ["u1"]
    continuity.assert_awaited_once_with(db, "c1")
    assert db.get_calls and db.get_calls[0][1] == "m1"
    assert len(db.executed) == 1, "必须按 message id 执行本地 SubconsciousLog purge"


def test_delete_cleanup_runs_even_if_apply_skipped(monkeypatch):
    """删除事件对端没有行（SKIPPED）时，本地仍可能残留待推 create——必须清队列。"""
    c = sc.SyncClient()
    db = _FakeDB()
    cleaned = AsyncMock()
    monkeypatch.setattr(c, "_clear_queued_events", cleaned)
    monkeypatch.setattr(sc, "apply_change", AsyncMock(return_value="skipped"))
    monkeypatch.setattr(
        "app.services.memory_retrieval_service.invalidate_session_cache_for_user",
        lambda uid: None,
    )
    monkeypatch.setattr(
        "app.services.context_continuity_service.invalidate_continuity", AsyncMock()
    )

    asyncio.run(c._apply_remote(db, _user(), None, _ev("delete")))
    cleaned.assert_awaited_once()


def test_non_delete_does_not_touch_delete_cleanup(monkeypatch):
    c = sc.SyncClient()
    db = _FakeDB()
    cleaned = AsyncMock()
    monkeypatch.setattr(c, "_clear_queued_events", cleaned)
    monkeypatch.setattr(sc, "apply_change", AsyncMock(return_value=sc.APPLIED))
    ev = {"entity_type": "messages", "entity_id": "m1", "op": "create",
          "payload": {"conversation_id": "c1"}, "origin_device": None}

    asyncio.run(c._apply_remote(db, _user(), None, ev))
    cleaned.assert_not_awaited()


def test_resync_message_tombstone_purges_local_derivatives(monkeypatch):
    """离线客户端走 resync 墓碑（P3 backfill 主路径）时也必须清本地派生数据。"""
    c = sc.SyncClient()
    db = _FakeDB()
    state = SimpleNamespace(pull_cursor=0)
    body = {
        "entities": {},
        "tombstones": [{"entity_type": "messages", "entity_id": "m1",
                        "deleted_at": "2026-11-05T00:00:00Z"}],
        "cursor": 9,
    }
    monkeypatch.setattr(c, "_call",
                        AsyncMock(return_value=_Resp(body)))
    monkeypatch.setattr(sc, "apply_change", AsyncMock(return_value=sc.APPLIED))
    cleared = AsyncMock()
    monkeypatch.setattr(c, "_clear_queued_events", cleared)
    purged = AsyncMock()
    monkeypatch.setattr(c, "_purge_local_message_derivatives", purged)

    asyncio.run(c._resync(db, None, _user(), state))

    purged.assert_awaited_once()
    assert purged.await_args.args[2] == ["m1"]
    assert purged.await_args.args[3] == "c1"
    assert state.pull_cursor == 9
