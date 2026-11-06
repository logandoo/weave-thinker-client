"""TDD — per-user MCP servers: CRUD, masking, owner isolation, hot registration.

Contract:
  U1  create/list/update/delete are owner-scoped; another user cannot read or
      mutate a row (404-equivalent / None).
  U2  Validation: slug name, transport enum, required url/command, length and
      per-user count caps; duplicate name rejected.
  U3  Secrets never leave the API surface: api_key and env values are masked;
      masked update values keep the stored secret.
  U4  ensure_user_mcp_tools registers tools with owner_user_id and a
      user-unique tool name; tools are invisible/undispatchable to other users.
  U5  update/delete re-diff the server (old tools unregistered, stdio session
      stopped).
"""
import asyncio
import os
import sys
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import database as db_mod

FAKE_SERVER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "fake_mcp_stdio_server.py"
)


class _AsyncResult:
    def __init__(self, result):
        self._result = result

    def scalars(self):
        return self._result.scalars()

    def scalar_one_or_none(self):
        return self._result.scalar_one_or_none()

    @property
    def rowcount(self):
        return self._result.rowcount


class _AsyncSessionFacade:
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

    async def flush(self):
        self._sync.flush()

    async def commit(self):
        self._sync.commit()

    async def rollback(self):
        self._sync.rollback()

    async def refresh(self, row):
        self._sync.refresh(row)

    async def delete(self, row):
        self._sync.delete(row)


def _make_db(monkeypatch):
    assert hasattr(db_mod, "UserMcpServer"), "UserMcpServer model is required"
    engine = create_engine("sqlite://", poolclass=StaticPool)
    tables = [
        db_mod.Base.metadata.tables[name]
        for name in ("users", "user_mcp_servers")
    ]
    db_mod.Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, lambda: _AsyncSessionFacade(factory())


@pytest.fixture()
def svc(monkeypatch):
    import app.services.user_mcp_service as svc
    import app.tools.mcp_client as mc
    # Hermetic trust switches: the test exercises stdio + loopback HTTP.
    from app.core.config import get_config as _get_cfg
    _cfg_cls = type(_get_cfg())
    monkeypatch.setattr(_cfg_cls, "user_mcp_stdio_enabled", property(lambda self: True))
    monkeypatch.setattr(_cfg_cls, "user_mcp_allow_private_hosts", property(lambda self: True))

    monkeypatch.setattr(mc, "_USER_MCP_STATE", {}, raising=False)
    monkeypatch.setattr(mc, "_STDIO_SESSIONS", {})
    monkeypatch.setattr(mc, "_STDIO_SERVER_SPECS", {})
    monkeypatch.setattr(svc, "_USER_CACHE", {}, raising=False)
    engine, factory = _make_db(monkeypatch)
    yield svc, factory, mc
    engine.dispose()


def _payload(name="mytools"):
    return {
        "name": name,
        "transport": "stdio",
        "command": sys.executable,
        "args": [FAKE_SERVER],
        "env": {"DEMO_SETTING": "visible-value"},
        "enabled": True,
    }


def _seed_user(factory, username="u1"):
    async def _run():
        async with factory() as db:
            uid = str(uuid.uuid4())
            db.add(db_mod.User(id=uid, username=username, password_hash="x"))
            await db.commit()
            return uid
    return asyncio.run(_run())


def test_create_masks_secrets_and_lists_owner_only(svc):
    service, factory, mc = svc
    uid = _seed_user(factory)
    other = _seed_user(factory, "u2")

    async def flow():
        async with factory() as db:
            created = await service.create_server(db, uid, _payload())
            listed = await service.list_servers(db, uid)
            other_list = await service.list_servers(db, other)
            return created, listed, other_list

    created, listed, other_list = asyncio.run(flow())
    assert created["name"] == "mytools"
    assert created["api_key"] in ("", None) or created["api_key"] == "***"
    assert created["env"]["DEMO_SETTING"] == "***"
    assert "visible-value" not in str(created)
    assert [r["name"] for r in listed] == ["mytools"]
    assert other_list == []


def test_create_validation(svc):
    service, factory, mc = svc
    uid = _seed_user(factory)

    async def flow():
        async with factory() as db:
            bad = []
            for payload in (
                {**_payload("bad name"), "name": "bad name!"},
                {**_payload(), "transport": "carrier-pigeon"},
                {**_payload(), "command": ""},
                {**_payload(), "url": "ftp://example.com"},
            ):
                try:
                    await service.create_server(db, uid, payload)
                    bad.append("accepted")
                except ValueError:
                    bad.append("rejected")
            await service.create_server(db, uid, _payload())
            try:
                await service.create_server(db, uid, _payload())
                bad.append("dup-accepted")
            except ValueError:
                bad.append("dup-rejected")
            return bad

    assert asyncio.run(flow()) == [
        "rejected", "rejected", "rejected", "rejected", "dup-rejected",
    ]


def test_ensure_registers_owner_scoped_tools(svc):
    service, factory, mc = svc
    uid = _seed_user(factory)
    other = _seed_user(factory, "u2")

    async def flow():
        async with factory() as db:
            created = await service.create_server(db, uid, _payload())
            runtime = await service.ensure_user_mcp_tools(db, uid, force=True)
            return created, runtime

    created, runtime = asyncio.run(flow())
    assert runtime["registered"] >= 1
    names = [n for n in mc.registry.get_all_registered_names() if n.endswith("_echo")]
    prefix = f"mcp_u{uid.replace('-', '')[:8]}_mytools"
    owned = [n for n in names if n.startswith(prefix)]
    assert owned, names
    name = owned[0]
    entry = mc.registry.get_entry(name)
    assert entry is not None and entry.owner_user_id == uid
    assert name not in mc.registry.get_all_tool_names(user_id=other)
    assert entry.toolset == "mcp-mytools"

    # Dispatch from another user is fail-closed; the owner succeeds.
    out_other = asyncio.run(mc.registry.dispatch(name, {"message": "x"}, user=type("U", (), {"id": other})()))
    assert "Unknown tool" in out_other
    out_owner = asyncio.run(mc.registry.dispatch(name, {"message": "x"}, user=type("U", (), {"id": uid})()))
    assert "echo:x" in out_owner


def test_update_replaces_tools_and_delete_stops_session(svc):
    service, factory, mc = svc
    uid = _seed_user(factory)

    async def flow():
        async with factory() as db:
            created = await service.create_server(db, uid, _payload())
            await service.ensure_user_mcp_tools(db, uid, force=True)
            sid = created["id"]
            prefix = f"mcp_u{uid.replace('-', '')[:8]}_mytools"
            old_names = [n for n in mc.registry.get_all_registered_names() if n.startswith(prefix)]
            old_session = mc._STDIO_SESSIONS.get(f"u:{uid}:mytools")
            updated = await service.update_server(
                db, uid, sid, {"enabled": False}
            )
            after_disable = [n for n in mc.registry.get_all_registered_names() if n.startswith(prefix)]
            await service.delete_server(db, uid, sid)
            return old_names, old_session, updated, after_disable

    old_names, old_session, updated, after_disable = asyncio.run(flow())
    assert old_names
    assert old_session is not None and old_session.proc is not None
    assert updated["enabled"] is False
    assert after_disable == []
    assert mc.registry.get_entry(old_names[0]) is None
    assert old_session.proc.returncode is not None
