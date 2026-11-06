"""PLAN T1 — 空会话 execution-target 绑定端点（API.md 文档驱动 TDD）。

Run from backend/: python -m pytest tests/test_exec_target_binding.py -q
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import BigInteger, create_engine, event, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _jsonb_as_json(element, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_as_int(element, compiler, **kw):
    return "INTEGER"

from app.db.database import (
    Base,
    ClientExecutorDevice,
    ClientExecutorDeviceTombstone,
    Conversation,
    Message,
    SyncEvent,
    User,
)
from app.services import client_device_service as cds


_ENGINES = []


class _Bridge:
    def __init__(self, sync_session):
        self._session = sync_session

    async def execute(self, statement):
        return self._session.execute(statement)

    async def get(self, model, ident):
        return self._session.get(model, ident)

    async def flush(self):
        self._session.flush()

    async def commit(self):
        self._session.commit()

    async def rollback(self):
        self._session.rollback()

    async def refresh(self, instance):
        self._session.refresh(instance)

    def add(self, instance):
        self._session.add(instance)


@pytest.fixture(autouse=True)
def _dispose_engines():
    yield
    while _ENGINES:
        _ENGINES.pop().dispose()


def _fk_closure_tables(*roots):
    tables, stack, seen = [], list(roots), set()
    while stack:
        table = stack.pop()
        if table.name in seen:
            continue
        seen.add(table.name)
        tables.append(table)
        for column in table.columns:
            for foreign_key in column.foreign_keys:
                stack.append(foreign_key.column.table)
    return tables


def _fresh(user_id="u-exec-owner-0001"):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _ENGINES.append(engine)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(
        engine,
        tables=_fk_closure_tables(
            ClientExecutorDevice.__table__,
            ClientExecutorDeviceTombstone.__table__,
            Conversation.__table__,
            Message.__table__,
            SyncEvent.__table__,
        ),
    )
    session = Session(engine)
    user = User(id=user_id, username="exec-owner", password_hash="x", is_active=True)
    session.add(user)
    session.commit()
    return session, _Bridge(session), user


def _client(db, user):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import conversation
    from app.core.deps import get_current_user
    from app.db.database import get_db

    app = FastAPI()
    app.include_router(conversation.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _register(db, user_id):
    code = cds.issue_pairing_code(user_id)["code"]
    row, _token = asyncio.run(cds.register_device(db, code=code, name="dev", platform="mac"))
    return row


def _conversation(session, user_id, target=None):
    conversation = Conversation(user_id=user_id, title="binding-test", execution_target=target)
    session.add(conversation)
    session.commit()
    return conversation


def _stored_target(session, conversation_id):
    session.expire_all()
    return session.execute(
        select(Conversation.execution_target).where(Conversation.id == conversation_id)
    ).scalar_one()


def test_set_device_binding():
    session, db, user = _fresh()
    device = _register(db, user.id)
    conversation = _conversation(session, user.id)

    response = _client(db, user).post(
        f"/api/conversations/{conversation.id}/execution-target",
        json={"target": f"device:{device.device_id}"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"execution_target": f"device:{device.device_id}"}
    assert _stored_target(session, conversation.id) == f"device:{device.device_id}"


def test_change_device_binding_while_conversation_is_empty():
    session, db, user = _fresh()
    first = _register(db, user.id)
    second = _register(db, user.id)
    conversation = _conversation(session, user.id, f"device:{first.device_id}")

    response = _client(db, user).post(
        f"/api/conversations/{conversation.id}/execution-target",
        json={"target": f"device:{second.device_id}"},
    )

    assert response.status_code == 200, response.text
    assert _stored_target(session, conversation.id) == f"device:{second.device_id}"


def test_messages_lock_execution_target():
    session, db, user = _fresh()
    first = _register(db, user.id)
    second = _register(db, user.id)
    conversation = _conversation(session, user.id, f"device:{first.device_id}")
    session.add(Message(conversation_id=conversation.id, role="user", content="already sent"))
    session.commit()

    response = _client(db, user).post(
        f"/api/conversations/{conversation.id}/execution-target",
        json={"target": f"device:{second.device_id}"},
    )

    assert response.status_code == 400, response.text
    assert "执行环境已固定" in response.json()["detail"]
    assert _stored_target(session, conversation.id) == f"device:{first.device_id}"


def test_other_users_conversation_is_not_readable_or_bindable():
    session, db, user = _fresh()
    device = _register(db, user.id)
    other = User(id="u-exec-other-0002", username="other", password_hash="x", is_active=True)
    session.add(other)
    session.commit()
    conversation = _conversation(session, other.id)

    response = _client(db, user).post(
        f"/api/conversations/{conversation.id}/execution-target",
        json={"target": f"device:{device.device_id}"},
    )

    assert response.status_code in {403, 404}, response.text
    assert response.json()["detail"] == "Conversation not found"
    assert _stored_target(session, conversation.id) is None


def test_null_clears_device_binding_to_cloud():
    session, db, user = _fresh()
    device = _register(db, user.id)
    conversation = _conversation(session, user.id, f"device:{device.device_id}")

    response = _client(db, user).post(
        f"/api/conversations/{conversation.id}/execution-target",
        json={"target": None},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"execution_target": None}
    assert _stored_target(session, conversation.id) is None


def test_unknown_and_revoked_devices_fail_closed_without_persistence():
    session, db, user = _fresh()
    conversation = _conversation(session, user.id)
    client = _client(db, user)

    unknown = client.post(
        f"/api/conversations/{conversation.id}/execution-target",
        json={"target": "device:unknown-device"},
    )
    assert unknown.status_code == 400, unknown.text
    assert _stored_target(session, conversation.id) is None

    revoked = _register(db, user.id)
    revoked.revoked = True
    session.commit()
    response = client.post(
        f"/api/conversations/{conversation.id}/execution-target",
        json={"target": f"device:{revoked.device_id}"},
    )
    assert response.status_code == 400, response.text
    assert _stored_target(session, conversation.id) is None


def test_first_stream_adopt_regression():
    session, db, user = _fresh()
    device = _register(db, user.id)
    conversation = _conversation(session, user.id)

    from app.api.chat import _resolve_execution_target_for_conversation

    asyncio.run(
        _resolve_execution_target_for_conversation(
            db, user, conversation, f"device:{device.device_id}"
        )
    )

    assert conversation.execution_target == f"device:{device.device_id}"


def test_create_window_binding_and_idempotent_rebind():
    session, db, user = _fresh()
    device = _register(db, user.id)
    client = _client(db, user)
    target = f"device:{device.device_id}"

    created = client.post(
        "/api/conversations",
        json={"title": "W1", "execution_target": target},
    )
    assert created.status_code == 200, created.text
    conversation_id = created.json()["id"]
    assert created.json()["execution_target"] == target

    rebound = client.post(
        f"/api/conversations/{conversation_id}/execution-target",
        json={"target": target},
    )
    assert rebound.status_code == 200, rebound.text
    assert rebound.json() == {"execution_target": target}
    assert _stored_target(session, conversation_id) == target


def test_nonempty_conversation_cannot_adopt_late_target_from_stream():
    session, db, user = _fresh()
    device = _register(db, user.id)
    conversation = _conversation(session, user.id)
    session.add(Message(conversation_id=conversation.id, role="user", content="old message"))
    session.commit()

    from fastapi import HTTPException
    from app.api.chat import _resolve_execution_target_for_conversation

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            _resolve_execution_target_for_conversation(
                db, user, conversation, f"device:{device.device_id}"
            )
        )
    assert error.value.status_code == 400
    assert "执行环境已固定" in error.value.detail
    assert conversation.execution_target is None


def test_binding_emits_sync_update_for_other_devices():
    session, db, user = _fresh()
    device = _register(db, user.id)
    conversation = _conversation(session, user.id)

    from app.core.config import get_config
    from app.services import sync_capture

    config = get_config()
    old_enabled = config._config.setdefault("sync", {}).get("enabled")
    was_registered = sync_capture._registered
    config._config["sync"]["enabled"] = True
    sync_capture.register_sync_capture()
    try:
        response = _client(db, user).post(
            f"/api/conversations/{conversation.id}/execution-target",
            json={"target": f"device:{device.device_id}"},
        )
        assert response.status_code == 200, response.text
        events = session.execute(
            select(SyncEvent).where(
                SyncEvent.entity_type == "conversations",
                SyncEvent.entity_id == conversation.id,
                SyncEvent.op == "update",
            )
        ).scalars().all()
        assert len(events) == 1
        payload = events[0].payload
        if isinstance(payload, str):
            payload = json.loads(payload)
        assert payload["execution_target"] == f"device:{device.device_id}"
    finally:
        if not was_registered:
            sync_capture.unregister_sync_capture()
        if old_enabled is None:
            config._config["sync"].pop("enabled", None)
        else:
            config._config["sync"]["enabled"] = old_enabled


def _sync_update(db, user, conversation_id, target, *, seconds=1, title=None):
    from app.services.sync_apply import apply_change

    payload = {
        "execution_target": target,
        "updated_at": (datetime.utcnow() + timedelta(seconds=seconds)).isoformat(),
    }
    if title is not None:
        payload["title"] = title
    return asyncio.run(
        apply_change(
            db,
            user,
            "conversations",
            conversation_id,
            "update",
            payload,
        )
    )


def test_sync_allows_valid_target_on_empty_conversation():
    session, db, user = _fresh()
    device = _register(db, user.id)
    conversation = _conversation(session, user.id)

    result = _sync_update(db, user, conversation.id, f"device:{device.device_id}")

    assert result == "applied"
    assert conversation.execution_target == f"device:{device.device_id}"


def test_sync_rejects_target_change_when_messages_exist():
    session, db, user = _fresh()
    first = _register(db, user.id)
    second = _register(db, user.id)
    conversation = _conversation(session, user.id, f"device:{first.device_id}")
    session.add(Message(conversation_id=conversation.id, role="assistant", content="old answer"))
    session.commit()

    result = _sync_update(db, user, conversation.id, f"device:{second.device_id}")

    assert result == "skipped"
    assert conversation.execution_target == f"device:{first.device_id}"


def test_sync_rejects_unknown_device_without_persistence():
    session, db, user = _fresh()
    conversation = _conversation(session, user.id)

    result = _sync_update(db, user, conversation.id, "device:unknown-device")

    assert result == "skipped"
    assert conversation.execution_target is None


def test_sync_same_target_is_idempotent_even_with_messages():
    session, db, user = _fresh()
    device = _register(db, user.id)
    target = f"device:{device.device_id}"
    conversation = _conversation(session, user.id, target)
    session.add(Message(conversation_id=conversation.id, role="user", content="old message"))
    session.commit()

    result = _sync_update(db, user, conversation.id, target)

    assert result == "applied"
    assert conversation.execution_target == target


def test_sync_same_target_after_revocation_preserves_other_field_updates():
    session, db, user = _fresh()
    device = _register(db, user.id)
    target = f"device:{device.device_id}"
    conversation = _conversation(session, user.id, target)
    conversation.title = "old-title"
    session.add(Message(conversation_id=conversation.id, role="user", content="old message"))
    device.revoked = True
    session.commit()

    result = _sync_update(db, user, conversation.id, target, title="new-title")

    assert result == "applied"
    assert conversation.title == "new-title"
    assert conversation.execution_target == target
