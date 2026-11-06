"""TDD — agent `mcp_manage` tool (2026-11-01, per-user MCP without restart).

The conversational install path must be first-class: a user asks the agent to
add an MCP server; the tool writes the owner-scoped row, registers the tools in
the current worker, and the NEXT iteration can call them. No restart.

Contract:
  M1  add → row + runtime registration + owner-scoped tool names.
  M2  list → masked secrets, only the calling user's rows.
  M3  remove → row gone + tools unregistered + stdio session stopped.
  M4  a connection failure is reported honestly (no fake success).
"""
import asyncio
import json
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


class _R:
    def __init__(self, r):
        self._r = r

    def scalars(self):
        return self._r.scalars()

    def scalar_one_or_none(self):
        return self._r.scalar_one_or_none()

    @property
    def rowcount(self):
        return self._r.rowcount


class _DB:
    def __init__(self, s):
        self._s = s

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        self._s.close()
        return False

    async def get(self, m, pk):
        return self._s.get(m, pk)

    def add(self, row):
        self._s.add(row)

    async def execute(self, stmt):
        return _R(self._s.execute(stmt))

    async def flush(self):
        self._s.flush()

    async def commit(self):
        self._s.commit()

    async def rollback(self):
        self._s.rollback()

    async def refresh(self, row):
        self._s.refresh(row)

    async def delete(self, row):
        self._s.delete(row)


@pytest.fixture()
def env(monkeypatch):
    import app.services.user_mcp_service as svc
    import app.tools.mcp_client as mc
    from app.tools.mcp_manage import mcp_manage_handler
    # Hermetic trust switches: the test exercises stdio + loopback HTTP.
    from app.core.config import get_config as _get_cfg
    _cfg_cls = type(_get_cfg())
    monkeypatch.setattr(_cfg_cls, "user_mcp_stdio_enabled", property(lambda self: True))
    monkeypatch.setattr(_cfg_cls, "user_mcp_allow_private_hosts", property(lambda self: True))

    assert hasattr(db_mod, "UserMcpServer")
    monkeypatch.setattr(mc, "_USER_MCP_STATE", {}, raising=False)
    monkeypatch.setattr(mc, "_STDIO_SESSIONS", {})
    monkeypatch.setattr(mc, "_STDIO_SERVER_SPECS", {})
    monkeypatch.setattr(svc, "_USER_CACHE", {}, raising=False)
    engine = create_engine("sqlite://", poolclass=StaticPool)
    db_mod.Base.metadata.create_all(
        engine,
        tables=[db_mod.Base.metadata.tables[n] for n in ("users", "user_mcp_servers")],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory, mcp_manage_handler, mc
    engine.dispose()


def _uid(factory):
    async def _run():
        async with _DB(factory()) as db:
            uid = str(uuid.uuid4())
            db.add(db_mod.User(id=uid, username="mcp-manage", password_hash="x"))
            await db.commit()
            return uid
    return asyncio.run(_run())


def test_add_list_remove_via_tool(env):
    factory, handler, mc = env
    uid = _uid(factory)
    user = type("U", (), {"id": uid})()

    async def flow():
        async with _DB(factory()) as db:
            added = json.loads(await handler(
                {"action": "add", "name": "mytools", "transport": "stdio",
                 "command": sys.executable, "args": [FAKE_SERVER]},
                user=user, db=db,
            ))
            listed = json.loads(await handler({"action": "list"}, user=user, db=db))
            prefix = f"mcp_u{uid.replace('-', '')[:8]}_mytools"
            names = [n for n in mc.registry.get_all_registered_names() if n.startswith(prefix)]
            removed = json.loads(await handler(
                {"action": "remove", "name": "mytools"}, user=user, db=db,
            ))
            gone = [n for n in mc.registry.get_all_registered_names() if n.startswith(prefix)]
            return added, listed, names, removed, gone

    added, listed, names, removed, gone = asyncio.run(flow())
    assert added.get("ok") is True, added
    assert added["server"]["name"] == "mytools"
    assert names, "tool must be registered before the next iteration"
    assert [s["name"] for s in listed["servers"]] == ["mytools"]
    assert removed.get("ok") is True
    assert gone == []


def test_connection_failure_is_honest(env):
    factory, handler, mc = env
    uid = _uid(factory)
    user = type("U", (), {"id": uid})()

    async def flow():
        async with _DB(factory()) as db:
            return json.loads(await handler(
                {"action": "add", "name": "broken", "transport": "stdio",
                 "command": sys.executable, "args": ["/nonexistent/mcp-server.py"]},
                user=user, db=db,
            ))

    out = asyncio.run(flow())
    assert out.get("ok") is False
    assert out.get("error")
    assert not [n for n in mc.registry.get_all_registered_names() if "broken" in n]
