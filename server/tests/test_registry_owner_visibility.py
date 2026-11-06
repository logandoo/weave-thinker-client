"""TDD — owner-scoped tool visibility and dispatch (2026-11-01, user MCP).

Contract:
  O1  Global tools (owner_user_id=None) are visible to every user context and
      to the no-user context.
  O2  A user-owned tool is visible only when user_id matches exactly; the
      no-user context sees global tools only (fail-closed).
  O3  Visibility getters (definitions / names / toolsets / map) all apply the
      same owner rule.
  O4  dispatch() rejects a user-owned tool for another user (or no user) even
      when the caller knows its exact name — never executes the handler.
  O5  get_all_registered_names() remains the collision/management surface and
      intentionally ignores ownership.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.tools.registry import registry


GLOBAL = "ownertest_global"
U1 = "ownertest_u1"
U2 = "ownertest_u2"
TOOLSET = "mcp-ownertest"


@pytest.fixture()
def owner_tools():
    calls = {"global": 0, "u1": 0, "u2": 0}

    def _mk(name, owner, key):
        def _handler(args, **kwargs):
            calls[key] += 1
            return "{}"
        registry.register(
            name=name,
            toolset=TOOLSET if owner else "core",
            schema={"name": name, "description": name, "parameters": {"type": "object"}},
            handler=_handler,
            is_async=False,
            description=name,
            owner_user_id=owner,
        )

    _mk(GLOBAL, None, "global")
    _mk(U1, "user-1", "u1")
    _mk(U2, "user-2", "u2")
    try:
        yield calls
    finally:
        for name in (GLOBAL, U1, U2):
            registry.unregister(name)


def _names(defs):
    return {(d.get("function") or {}).get("name") for d in defs}


def test_definitions_filter_by_owner(owner_tools):
    u1 = _names(registry.get_definitions(user_id="user-1"))
    u2 = _names(registry.get_definitions(user_id="user-2"))
    anon = _names(registry.get_definitions(user_id=None))
    assert {GLOBAL, U1} <= u1 and U2 not in u1
    assert {GLOBAL, U2} <= u2 and U1 not in u2
    assert GLOBAL in anon and U1 not in anon and U2 not in anon


def test_visibility_getters_share_owner_rule(owner_tools):
    assert set(registry.get_all_tool_names(user_id="user-1")) >= {GLOBAL, U1}
    assert U2 not in registry.get_all_tool_names(user_id="user-1")
    assert set(registry.get_tool_names_for_toolset(TOOLSET, user_id="user-1")) == {U1}
    assert registry.get_toolset_for_tool(U2, user_id="user-1") is None
    assert registry.get_toolset_for_tool(U2, user_id="user-2") == TOOLSET
    visible_map = registry.get_tool_to_toolset_map(user_id="user-1")
    assert U1 in visible_map and U2 not in visible_map and GLOBAL in visible_map
    # Collision/management surface sees everything.
    assert {GLOBAL, U1, U2} <= set(registry.get_all_registered_names())


def test_dispatch_fails_closed_for_other_owner(owner_tools):
    # Correct owner executes.
    asyncio.run(registry.dispatch(U1, {}, user=SimpleNamespace(id="user-1")))
    assert owner_tools["u1"] == 1

    # Another user / no user never reaches the handler.
    asyncio.run(registry.dispatch(U1, {}, user=SimpleNamespace(id="user-2")))
    asyncio.run(registry.dispatch(U1, {}))
    assert owner_tools["u1"] == 1

    # Global tool executes in both contexts.
    asyncio.run(registry.dispatch(GLOBAL, {}, user=SimpleNamespace(id="user-2")))
    asyncio.run(registry.dispatch(GLOBAL, {}))
    assert owner_tools["global"] == 2
