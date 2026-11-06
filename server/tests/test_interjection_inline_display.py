"""TDD — interjection inline display boundaries (2026-11-01).

Root symptom (conv 861cb5af, user report): interjections were persisted as
top-level user rows while the single assistant answer was created at run start
and anchor-bumped to after the last interjection → transcript showed the
original query followed by a stack of user queries, then one answer.

Contract tested here:
  I1  At each commit, pending reasoning/text is flushed into the persisted
      display_sequence/content_segments BEFORE the interjection item, so the
      item marks the real position in the run.
  I2  The interjection item carries message_id/content/created_at plus
      after_index/after_char (position in display sequence and joined content).
  I3  Multiple commits are append-only and keep their order.
  I4  The terminal resolver preserves interjection items between text/tool
      placeholders (never drops them when mapping tool_placeholders).
  I5  Terminal persistence writes Message.interjection_items and the user row's
      embedded_in_message_id.

Run: cd backend && python -m pytest tests/test_interjection_inline_display.py -v
"""
import asyncio
import inspect
import json
import re
from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.interjection_display import (
    SEGMENT_SEPARATOR,
    append_interjection_item,
    flush_pending_text,
    resolve_persisted_display_sequence,
)
from app.api.chat_persistence import _save_assistant_terminal
from app.db.database import Base, Conversation, Message, User


class _PF:
    def __init__(self):
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1


class _Run:
    def __init__(self):
        self.assistant_content = ""
        self.current_reasoning_segment = ""
        self.content_segments = []
        self.display_sequence = []
        self.interjection_items = []
        self.stitch_active = True
        self.stitch_base = "old"
        self.pf = _PF()


def _item(msg_id="ij-1", content="插话", created_at="2026-11-01T11:22:47"):
    return {
        "message_id": msg_id,
        "content": content,
        "created_at": created_at,
    }


def test_flush_pending_text_places_reasoning_and_text_before_interjection():
    run = _Run()
    run.current_reasoning_segment = "思考"
    run.assistant_content = "第一段"
    run.display_sequence = [{"type": "tool_placeholder"}]

    item = append_interjection_item(run, **_item())

    assert [x["type"] for x in run.display_sequence] == [
        "tool_placeholder", "reasoning_step", "text", "interjection",
    ]
    assert run.display_sequence[1]["content"] == "思考"
    assert run.display_sequence[2]["content"] == "第一段"
    assert run.content_segments == ["第一段"]
    assert run.assistant_content == ""
    assert run.current_reasoning_segment == ""
    assert run.stitch_active is False and run.stitch_base == ""
    assert run.pf.reset_calls == 1
    assert item["after_index"] == 3
    assert item["after_char"] == len("第一段")
    assert run.interjection_items == [item]


def test_second_interjection_after_no_new_text_keeps_order_and_offset():
    run = _Run()
    run.assistant_content = "A"
    first = append_interjection_item(run, **_item("ij-1", "one"))
    second = append_interjection_item(run, **_item("ij-2", "two", "2026-11-01T11:31:05"))

    assert [x.get("message_id") for x in run.display_sequence if x["type"] == "interjection"] == [
        "ij-1", "ij-2",
    ]
    assert second["after_index"] == len(run.display_sequence) - 1
    assert second["after_char"] == first["after_char"] == len("A")
    assert [x["message_id"] for x in run.interjection_items] == ["ij-1", "ij-2"]


def test_after_char_matches_joined_content_boundary():
    run = _Run()
    run.assistant_content = "seg1"
    first = append_interjection_item(run, **_item())
    run.assistant_content = "seg2"
    second = append_interjection_item(run, **_item("ij-2", "two"))

    # The persisted final content joins segments with the canonical separator.
    joined = SEGMENT_SEPARATOR.join(run.content_segments + ["final"])
    cut = joined[: second["after_char"]]
    assert cut == SEGMENT_SEPARATOR.join(["seg1", "seg2"])
    assert first["after_char"] == len("seg1")


def test_resolver_preserves_interjection_between_tool_placeholders():
    sequence = [
        {"type": "reasoning_step", "content": "r"},
        {"type": "text", "content": "before"},
        {"type": "interjection", "message_id": "ij-1", "content": "插话"},
        {"type": "tool_placeholder"},
        {"type": "text", "content": "after"},
    ]
    steps = [{"name": "call_1", "title": "工具", "content": "{}", "step_type": "tool"}]
    resolved = resolve_persisted_display_sequence(sequence, steps)
    assert [x["type"] for x in resolved] == [
        "reasoning_step", "text", "interjection", "tool", "text",
    ]
    assert resolved[2]["message_id"] == "ij-1"


def test_after_char_uses_utf16_code_units_for_emoji():
    """Frontend slices JS strings (UTF-16); an emoji before the boundary is 2."""
    run = _Run()
    run.assistant_content = "😀"
    item = append_interjection_item(run, **_item())
    assert item["after_char"] == 2, "emoji must count as two UTF-16 units"


def test_flush_with_whitespace_does_not_create_empty_text_item():
    run = _Run()
    run.assistant_content = "   \n  "
    item = append_interjection_item(run, **_item())
    assert run.content_segments == []
    assert [x["type"] for x in run.display_sequence] == ["interjection"]
    assert item["after_char"] == 0


# ── terminal persistence (SQLite in-memory, same bridge pattern as
#    test_interjection_message_ordering.py) ────────────────────────────────

class _Bridge:
    def __init__(self, sync_sess):
        self._s = sync_sess

    async def execute(self, stmt):
        return self._s.execute(stmt)

    async def commit(self):
        self._s.commit()


def _sqlite_tables():
    tables, stack, seen = [], [Conversation.__table__, Message.__table__], set()
    while stack:
        t = stack.pop()
        if t.name in seen:
            continue
        seen.add(t.name)
        tables.append(t)
        for col in t.columns:
            for fk in col.foreign_keys:
                stack.append(fk.column.table)
    return tables


def _session_with_rows():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=_sqlite_tables())
    sess = Session(engine)
    sess.add(User(id="u1", username="t", password_hash="x"))
    sess.add(Conversation(id="c1", user_id="u1", title="t"))
    sess.add(Message(
        id="m-assistant", conversation_id="c1", role="assistant",
        content="", delivery_status="streaming",
        created_at=datetime(2026, 11, 1, 11, 13, 35),
        interjection_items=None,
    ))
    sess.add(Message(
        id="m-user-ij", conversation_id="c1", role="user",
        content="插话", delivery_status="final", created_at=datetime(2026, 11, 1, 11, 22, 47),
        embedded_in_message_id="m-assistant",
    ))
    sess.commit()
    return sess


def test_terminal_save_writes_interjection_items_column():
    sess = _session_with_rows()
    items = [dict(_item("m-user-ij", "插话"), after_index=1, after_char=0)]

    asyncio.run(_save_assistant_terminal(
        _Bridge(sess), conversation_id="c1", content="最终回答",
        reasoning=None, tool_results=None, tool_calls=None,
        context_info=None, status="final", row_id="m-assistant",
        interjection_items=json.dumps(items, ensure_ascii=False),
    ))
    sess.commit()
    row = sess.execute(select(Message).where(Message.id == "m-assistant")).scalar_one()
    assert json.loads(row.interjection_items) == items
    user = sess.execute(select(Message).where(Message.id == "m-user-ij")).scalar_one()
    assert user.embedded_in_message_id == "m-assistant"


def test_interjection_items_from_buffer_dedupes():
    from app.api.chat_persistence import interjection_items_from_buffer

    class _Buf:
        interjections_committed = [
            {"display_item": {"message_id": "ij-1", "content": "a", "after_index": 0, "after_char": 0}},
            {"display_item": {"message_id": "ij-1", "content": "a", "after_index": 0, "after_char": 0}},
            {"display_item": {"message_id": "ij-2", "content": "b", "after_index": 2, "after_char": 3}},
            {"display_item": None},
        ]

    items = json.loads(interjection_items_from_buffer(_Buf()))
    assert [x["message_id"] for x in items] == ["ij-1", "ij-2"]
    assert items[1]["after_char"] == 3
    assert interjection_items_from_buffer(None) is None


def test_every_terminal_save_bound_to_interjection_items():
    """Source-level pin: every terminal save must pass the run's items."""
    from app.api import chat_stream_transport as transport

    blocks = re.findall(
        r"_save_assistant_terminal\(\n(.*?)\n\s*\)\n",
        inspect.getsource(transport),
        re.S,
    )
    assert len(blocks) == 6, f"expected 6 terminal saves, got {len(blocks)}"
    for idx, block in enumerate(blocks):
        assert "interjection_items=" in block, f"terminal save #{idx + 1} missing interjection_items"
    # Three calls have the run object and pass its full list; three legacy
    # consumers have no run and must reconstruct from the stream buffer.
    assert sum("interjection_items=(" in b for b in blocks) == 3
    assert sum("interjection_items_from_buffer" in b for b in blocks) == 3
    # The legacy resume save in chat.py was the 7th terminal surface.
    from app.api import chat as _chat

    chat_blocks = re.findall(
        r"_save_assistant_terminal\(\n(.*?)\n\s*\)\n",
        inspect.getsource(_chat),
        re.S,
    )
    assert len(chat_blocks) == 1
    assert "interjection_items=interjection_items_from_buffer" in chat_blocks[0]
