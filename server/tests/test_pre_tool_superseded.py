"""P0 R2: parked pre-tool prose is marked, never deleted."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.chat import _display_sequence_keep_evidence, _parked_prose_item


def test_parked_prose_item_is_superseded_and_keeps_text():
    item = _parked_prose_item("这是已经展示给用户的预工具草稿。")
    assert item == {
        "type": "text",
        "content": "这是已经展示给用户的预工具草稿。",
        "superseded": True,
    }
    assert item["content"]  # never empty out user-visible text


def test_display_sequence_keep_evidence_keeps_superseded_after_last_tool():
    seq = [
        {"type": "tool_call", "call_id": "c1"},
        {"type": "text", "content": "shown draft", "superseded": True},
        {"type": "text", "content": "rejected draft"},
    ]
    kept = _display_sequence_keep_evidence(seq)
    assert [it["content"] for it in kept if it.get("type") == "text"] == ["shown draft"]
