"""Persistent voice background-task notification outbox (hermetic SQLite)."""
import asyncio
import uuid
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import database as db_mod
from app.services import voice_service as vs


class _AsyncResult:
    def __init__(self, result):
        self._result = result

    @property
    def rowcount(self):
        return self._result.rowcount

    def scalars(self):
        return self._result.scalars()


class _AsyncSessionFacade:
    """Expose the small AsyncSession surface used by voice services over real SQLite."""

    def __init__(self, sync_session):
        self._sync = sync_session

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self._sync.close()
        return False

    async def get(self, model, pk):
        return self._sync.get(model, pk)

    def add(self, row):
        self._sync.add(row)

    async def execute(self, statement):
        return _AsyncResult(self._sync.execute(statement))

    async def commit(self):
        self._sync.commit()

    async def refresh(self, row):
        self._sync.refresh(row)


def _orm():
    assert hasattr(db_mod, "VoiceNotificationOutbox"), "VoiceNotificationOutbox model is required"
    return db_mod.VoiceNotificationOutbox


def _make_db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    names = ["users", "assistants", "conversations", "agent_tasks"]
    if hasattr(db_mod, "VoiceNotificationOutbox"):
        names.append("voice_notification_outbox")
    tables = [db_mod.Base.metadata.tables[name] for name in names]
    db_mod.Base.metadata.create_all(engine, tables=tables)
    sync_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, lambda: _AsyncSessionFacade(sync_factory())


async def _seed(factory, monkeypatch, voice=True):
    async with factory() as db:
        uid = str(uuid.uuid4())
        aid = str(uuid.uuid4())
        cid = str(uuid.uuid4())
        other_cid = str(uuid.uuid4())
        task_id = str(uuid.uuid4())
        db.add(db_mod.User(id=uid, username="voice-outbox", password_hash="x"))
        db.add(db_mod.Assistant(id=aid, user_id=uid, name="语音助理" if voice else "普通助手"))
        db.add(db_mod.Conversation(id=cid, user_id=uid, assistant_id=aid if voice else None, title="voice"))
        db.add(db_mod.Conversation(id=other_cid, user_id=uid, assistant_id=None, title="other"))
        db.add(db_mod.AgentTask(
            id=task_id, user_id=uid, conversation_id=cid if voice else other_cid,
            title="测试任务", goal="查结果", status="completed", result="真实结果",
        ))
        await db.commit()
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", factory)
    monkeypatch.setattr(vs, "_ACTIVE_VOICE_SESSIONS", {})
    return uid, cid, other_cid, task_id


def test_terminal_voice_task_is_persistent_and_idempotent_without_live_ws(monkeypatch):
    async def run():
        engine, factory = _make_db()
        try:
            _, _, _, task_id = await _seed(factory, monkeypatch, voice=True)
            assert await vs.notify_voice_task_finished(task_id) is False
            assert await vs.notify_voice_task_finished(task_id) is False
            async with factory() as db:
                rows = (await db.execute(select(_orm()))).scalars().all()
                assert len(rows) == 1, "无活跃 WS 也必须恰好一条 outbox"
                assert rows[0].status == "pending"
        finally:
            engine.dispose()

    asyncio.run(run())


def test_non_voice_task_does_not_create_outbox(monkeypatch):
    async def run():
        engine, factory = _make_db()
        try:
            _, _, _, task_id = await _seed(factory, monkeypatch, voice=False)
            assert await vs.notify_voice_task_finished(task_id) is False
            async with factory() as db:
                rows = (await db.execute(select(_orm()))).scalars().all()
                assert rows == []
        finally:
            engine.dispose()

    asyncio.run(run())


def test_pending_notice_replays_only_origin_conversation_once(monkeypatch):
    async def run():
        engine, factory = _make_db()
        try:
            uid, cid, _, task_id = await _seed(factory, monkeypatch, voice=True)
            await vs.notify_voice_task_finished(task_id)
            session = vs.VoiceDuplexSession(None, SimpleNamespace(id=uid, username="u"), None, conversation_id=cid)
            session._send_json = _noop
            assert hasattr(session, "_replay_pending_notices")
            await session._replay_pending_notices()
            await session._replay_pending_notices()
            assert session._turn_queue.qsize() == 1, "重复 replay 不得重复入队"
            item = await session._turn_queue.get()
            assert item["notice"]["task_id"] == task_id
            assert item["notification_id"], item
        finally:
            engine.dispose()

    asyncio.run(run())


def test_only_playback_drained_marks_notice_delivered(monkeypatch):
    async def run():
        engine, factory = _make_db()
        try:
            uid, cid, _, task_id = await _seed(factory, monkeypatch, voice=True)
            await vs.notify_voice_task_finished(task_id)
            session = vs.VoiceDuplexSession(None, SimpleNamespace(id=uid, username="u"), None, conversation_id=cid)
            session._send_json = _noop
            await session._replay_pending_notices()
            item = await session._turn_queue.get()
            notification_id = item["notification_id"]
            async with factory() as db:
                row = await db.get(_orm(), notification_id)
                assert row.status == "pending", "入队不等于 delivered"

            session._active_notification_id = notification_id
            session._active_notification_audio_sent = True
            session._active_notification_generation_complete = True
            await session._handle_client_event({"event": "playback_drained", "notification_id": notification_id})
            async with factory() as db:
                row = await db.get(_orm(), notification_id)
                assert row.status == "delivered"
                assert row.delivered_at is not None
        finally:
            engine.dispose()

    asyncio.run(run())


def test_interrupt_does_not_mark_notice_delivered(monkeypatch):
    async def run():
        engine, factory = _make_db()
        try:
            uid, cid, _, task_id = await _seed(factory, monkeypatch, voice=True)
            await vs.notify_voice_task_finished(task_id)
            session = vs.VoiceDuplexSession(None, SimpleNamespace(id=uid, username="u"), None, conversation_id=cid)
            session._send_json = _noop
            await session._replay_pending_notices()
            item = await session._turn_queue.get()
            notification_id = item["notification_id"]
            session._active_notification_id = notification_id
            await session._request_interrupt()
            async with factory() as db:
                row = await db.get(_orm(), notification_id)
                assert row.status == "pending", "确认打断不得误标已播完"
        finally:
            engine.dispose()

    asyncio.run(run())


def test_playback_drained_requires_notice_audio(monkeypatch):
    async def run():
        engine, factory = _make_db()
        try:
            uid, cid, _, task_id = await _seed(factory, monkeypatch, voice=True)
            await vs.notify_voice_task_finished(task_id)
            session = vs.VoiceDuplexSession(None, SimpleNamespace(id=uid, username="u"), None, conversation_id=cid)
            session._send_json = _noop
            await session._replay_pending_notices()
            item = await session._turn_queue.get()
            notification_id = item["notification_id"]
            session._active_notification_id = notification_id
            session._active_notification_audio_sent = False
            session._active_notification_generation_complete = True
            await session._handle_client_event({"event": "playback_drained", "notification_id": notification_id})
            async with factory() as db:
                row = await db.get(_orm(), notification_id)
                assert row.status == "pending", "未发出该通知 PCM 时，迟到 drained 不得确认"

            session._active_notification_id = notification_id
            session._active_notification_audio_sent = True
            session._active_notification_generation_complete = True
            session._notification_states[notification_id]["audio_sent"] = True
            await session._handle_client_event({"event": "playback_drained", "notification_id": notification_id})
            async with factory() as db:
                row = await db.get(_orm(), notification_id)
                assert row.status == "delivered"
        finally:
            engine.dispose()

    asyncio.run(run())


def test_live_notice_never_crosses_conversation(monkeypatch):
    async def run():
        engine, factory = _make_db()
        try:
            uid, cid, other_cid, task_id = await _seed(factory, monkeypatch, voice=True)
            calls = []

            class _Session:
                conversation_id = other_cid
                _closed = False

                async def _enqueue_bg_task_notice(self, info, notification_id=None):
                    calls.append((info, notification_id))

            monkeypatch.setattr(vs, "_ACTIVE_VOICE_SESSIONS", {uid: _Session()})
            assert await vs.notify_voice_task_finished(task_id) is False
            assert calls == [], "A 的任务不得在 B 的会话中播报或泄露结果"
            async with factory() as db:
                row = await db.get(_orm(), task_id)
                assert row.status == "pending"
        finally:
            engine.dispose()

    asyncio.run(run())


def test_delivered_notice_is_not_spoken_again_by_duplicate_hook(monkeypatch):
    async def run():
        engine, factory = _make_db()
        try:
            uid, cid, _, task_id = await _seed(factory, monkeypatch, voice=True)
            calls = []

            class _Session:
                conversation_id = cid
                _closed = False

                async def _enqueue_bg_task_notice(self, info, notification_id=None):
                    calls.append((info, notification_id))

            monkeypatch.setattr(vs, "_ACTIVE_VOICE_SESSIONS", {uid: _Session()})
            from app.services.voice_notification_service import mark_voice_notification_delivered
            async with factory() as db:
                task = await db.get(db_mod.AgentTask, task_id)
                from app.services.voice_notification_service import record_voice_task_notification
                await record_voice_task_notification(db, task)
                await mark_voice_notification_delivered(db, task_id)
            assert await vs.notify_voice_task_finished(task_id) is False
            assert calls == []
        finally:
            engine.dispose()

    asyncio.run(run())


def test_interrupt_allows_same_session_notice_retry(monkeypatch):
    async def run():
        engine, factory = _make_db()
        try:
            uid, cid, _, task_id = await _seed(factory, monkeypatch, voice=True)
            await vs.notify_voice_task_finished(task_id)
            session = vs.VoiceDuplexSession(None, SimpleNamespace(id=uid, username="u"), None, conversation_id=cid)
            session._send_json = _noop
            await session._replay_pending_notices()
            item = await session._turn_queue.get()
            notification_id = item["notification_id"]
            session._active_notification_id = notification_id
            await session._request_interrupt()
            assert notification_id not in session._queued_notice_ids
            from datetime import datetime, timedelta
            async with factory() as db:
                row = await db.get(_orm(), notification_id)
                row.last_attempt_at = datetime.utcnow() - timedelta(seconds=301)
                await db.commit()
            await session._replay_pending_notices()
            assert session._turn_queue.qsize() == 1, "pending 通知在同一会话也必须可重试"
        finally:
            engine.dispose()

    asyncio.run(run())


def test_ack_requires_matching_id_and_completed_notice_generation(monkeypatch):
    async def run():
        engine, factory = _make_db()
        try:
            uid, cid, _, task_id = await _seed(factory, monkeypatch, voice=True)
            await vs.notify_voice_task_finished(task_id)
            session = vs.VoiceDuplexSession(None, SimpleNamespace(id=uid, username="u"), None, conversation_id=cid)
            session._send_json = _noop
            await session._replay_pending_notices()
            item = await session._turn_queue.get()
            notification_id = item["notification_id"]
            session._active_notification_id = notification_id
            session._active_notification_audio_sent = True
            session._active_notification_generation_complete = False
            await session._handle_client_event({"event": "playback_drained", "notification_id": notification_id})
            async with factory() as db:
                row = await db.get(_orm(), notification_id)
                assert row.status == "pending", "通知生成未完整不得 delivered"

            session._active_notification_generation_complete = True
            await session._handle_client_event({"event": "playback_drained", "notification_id": "other"})
            async with factory() as db:
                row = await db.get(_orm(), notification_id)
                assert row.status == "pending", "其他通知的 drained 不得误确认"

            session._active_notification_id = notification_id
            session._active_notification_audio_sent = True
            session._active_notification_generation_complete = True
            session._notification_states[notification_id]["generation_complete"] = True
            await session._handle_client_event({"event": "playback_drained", "notification_id": notification_id})
            async with factory() as db:
                row = await db.get(_orm(), notification_id)
                assert row.status == "delivered"
        finally:
            engine.dispose()

    asyncio.run(run())


async def _noop(*args, **kwargs):
    return None
