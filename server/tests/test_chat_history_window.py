"""Regression: conversation history window must take the NEWEST messages.

Root cause (conv a104fbc5, 2026-09-20): `_load_conversation_messages(..., limit=200)`
ordered by created_at ASC and applied LIMIT directly, so it returned the OLDEST
200 of a 219-message conversation. The downstream `conv_msgs[-context_limit:]`
slice (40) then kept #161–#200 — the user's newest message never reached the
model, which continued stale deathmatch context for an hour and produced an
empty answer ("回答生成失败：模型在工具调用后未能产出有效回答").

Hermetic: the fake session emulates the SELECT's ORDER BY + LIMIT semantics
without touching PostgreSQL, so it runs in the CI subset too.

Run: cd backend && ../.venv/bin/python -m pytest tests/test_chat_history_window.py -v
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.chat import _load_conversation_messages  # noqa: E402


class _Row:
    def __init__(self, idx):
        self.id = f"m{idx:04d}"
        self.created_at = idx


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeDB:
    """Emulates only the SELECT semantics used by _load_conversation_messages:
    ORDER BY created_at [DESC] + optional LIMIT."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        limit = None
        _lc = getattr(stmt, "_limit_clause", None)
        if _lc is not None:
            try:
                limit = int(_lc.value)
            except Exception:
                limit = None
        desc = False
        for _ob in getattr(stmt, "_order_by_clauses", ()) or ():
            _mod = getattr(_ob, "modifier", None)
            if _mod is not None and getattr(_mod, "__name__", "") == "desc_op":
                desc = True
        rows = sorted(self._rows, key=lambda m: m.created_at, reverse=desc)
        if limit is not None:
            rows = rows[:limit]
        return _FakeResult(rows)


def _run(coro):
    return asyncio.run(coro)


def test_limit_returns_newest_messages_in_chronological_order():
    rows = [_Row(i) for i in range(1, 251)]  # 250 messages
    db = _FakeDB(rows)
    got = _run(_load_conversation_messages(db, "conv", limit=200))
    ids = [m.id for m in got]
    assert len(got) == 200
    assert ids[0] == "m0051", f"window must start at the 51st newest, got {ids[0]}"
    assert ids[-1] == "m0250", f"newest message must be last, got {ids[-1]}"
    assert ids == sorted(ids), "returned messages must stay chronological (ASC)"


def test_limit_below_message_count_keeps_newest_only():
    rows = [_Row(i) for i in range(1, 51)]
    db = _FakeDB(rows)
    got = _run(_load_conversation_messages(db, "conv", limit=40))
    ids = [m.id for m in got]
    assert ids[0] == "m0011" and ids[-1] == "m0050"


def test_no_limit_returns_all_ascending():
    rows = [_Row(i) for i in range(1, 31)]
    db = _FakeDB(rows)
    got = _run(_load_conversation_messages(db, "conv"))
    ids = [m.id for m in got]
    assert ids == [f"m{i:04d}" for i in range(1, 31)]


def test_newest_message_survives_downstream_context_slice():
    """The composed window (`[-context_limit:]` over the loaded list) must
    contain the newest message — the exact shape that broke conv a104fbc5."""
    rows = [_Row(i) for i in range(1, 251)]
    db = _FakeDB(rows)
    loaded = _run(_load_conversation_messages(db, "conv", limit=200))
    context_limit = 40
    window = loaded[-context_limit:]
    assert window[-1].id == "m0250"
    assert len(window) == 40
