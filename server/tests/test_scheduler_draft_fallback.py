"""P0 R2: a scheduled run with no final text must not hide its only answer."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.agent_scheduler import promote_last_draft_without_final


def test_last_superseded_draft_is_promoted_when_final_is_empty():
    seq = [
        {"type": "text", "content": "draft", "superseded": True},
        {"type": "tool_placeholder"},
    ]
    assert promote_last_draft_without_final(seq, "") is True
    assert seq[0]["superseded"] is False


def test_final_text_keeps_draft_collapsed():
    seq = [{"type": "text", "content": "draft", "superseded": True}]
    assert promote_last_draft_without_final(seq, "final") is False
    assert seq[0]["superseded"] is True
