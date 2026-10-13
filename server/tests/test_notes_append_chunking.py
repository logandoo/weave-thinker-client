"""TDD tests — notes 工具分块写入（append_note）（2026-09-21 波 2-B）。

长内容单次写入会被输出上限截断（conv fcb2e60a）。除「截断不派发」外，
工具侧需要真正的分块协议：create_note 写开头 + append_note 分批追加
（update_note 是全量覆盖，无法安全续写）。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_notes_append_chunking.py -v
"""
import asyncio
import json

from app.tools.notes import _append_note_content, _notes_tool_with_permission, notes_tool
from app.tools.registry import registry


class TestAppendContent:
    def test_empty_existing_returns_addition(self):
        assert _append_note_content("", "第一段") == "第一段"
        assert _append_note_content(None, "第一段") == "第一段"

    def test_normal_append_separates_with_blank_line(self):
        assert _append_note_content("第一段", "第二段") == "第一段\n\n第二段"

    def test_existing_single_newline_gets_one_more(self):
        assert _append_note_content("第一段\n", "第二段") == "第一段\n\n第二段"

    def test_existing_blank_line_no_extra(self):
        assert _append_note_content("第一段\n\n", "第二段") == "第一段\n\n第二段"

    def test_addition_leading_newline_no_extra(self):
        assert _append_note_content("第一段", "\n第二段") == "第一段\n第二段"


class _FakeNote:
    def __init__(self, note_id="n1", title="长文", content="开头"):
        self.id = note_id
        self.title = title
        self.content = content


class _FakeResult:
    def __init__(self, note):
        self._note = note

    def scalar_one_or_none(self):
        return self._note


class _FakeDB:
    def __init__(self, note):
        self._note = note
        self.committed = 0

    async def execute(self, *_a, **_k):
        return _FakeResult(self._note)

    async def commit(self):
        self.committed += 1

    async def refresh(self, _obj):
        return None


class _FakeUser:
    id = "u1"


class TestAppendNoteAction:
    def test_permission_gate_maps_to_note_edit(self):
        payload = json.loads(asyncio.run(_notes_tool_with_permission({
            "action": "append_note", "note_id": "n1", "content": "x",
        })))
        assert payload.get("_permission_needed") is True
        assert payload.get("_permission_key") == "note_edit"

    def test_append_persists_growing_content(self):
        note = _FakeNote(content="开头")
        db = _FakeDB(note)
        result = json.loads(asyncio.run(notes_tool(
            {"action": "append_note", "note_id": "n1", "content": "第二批内容", "_permission_granted": True},
            db=db, user=_FakeUser(),
        )))
        assert result.get("success") is True
        assert note.content == "开头\n\n第二批内容"
        assert db.committed == 1

    def test_append_requires_content(self):
        db = _FakeDB(_FakeNote())
        result = json.loads(asyncio.run(notes_tool(
            {"action": "append_note", "note_id": "n1", "content": "   ", "_permission_granted": True},
            db=db, user=_FakeUser(),
        )))
        assert "error" in result
        assert db.committed == 0


class TestRegistrySurface:
    def test_append_note_exposed_in_schema(self):
        defs = registry.get_definitions(["notes"], quiet=True)
        actions = []
        for d in defs:
            fn = d.get("function", d)
            actions.extend(
                fn.get("parameters", {}).get("properties", {}).get("action", {}).get("enum", [])
            )
        assert "append_note" in actions

    def test_agent_permission_map_covers_append_note(self):
        from app.services.agent_permissions import permission_key_for_tool_request

        assert permission_key_for_tool_request("notes", {"action": "append_note"}) == "note_edit"
