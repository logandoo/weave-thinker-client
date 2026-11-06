"""Memory injection boundary + self-reference guard (2026-10-18).

Production evidence (conv dca19082): the retrieval context contained
  - unrelated high-weight concepts (DeepSeek Harness, self-evolving agent,
    DeepSeek V4 specs) on every turn;
  - the current conversation's own last user messages as "subconscious"
    snippets and a same-day "event" summary.
The model then treated unrelated project incidents as facts of this turn.

Contracts:
  1. Retrieval drops candidates that are the current conversation's own
     recent text (prefix match) or were created inside the active window.
  2. The injected context carries an explicit boundary: background only,
     never evidence for the current conversation.
  3. The v1 fallback sections carry the same boundary.

Run: cd backend && python -m pytest tests/test_memory_injection_boundary_20261018.py -v
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.memory_retrieval_service import (
    RetrievalCandidate,
    _matches_recent_user_text,
    _recent_user_texts,
    _self_reference_guard,
    _within_recent_minutes,
)


def _cand(tier, text, created=None, narrative=None):
    meta = {"created_at": created or datetime.utcnow()}
    if narrative is not None:
        meta = {"narrative": narrative, "valid_from": created or datetime.utcnow()}
        text = narrative
    return RetrievalCandidate(id="c1", tier=tier, content=text, score=0.9, metadata=meta)


def test_recent_user_texts_extracts_latest_five_in_reverse_order():
    msgs = [
        {"role": "assistant", "content": "回答"},
        {"role": "user", "content": "第一条"},
        {"role": "user", "content": "第二条"},
    ]
    assert _recent_user_texts(msgs) == ["第二条", "第一条"]


def test_recent_user_texts_accepts_orm_rows():
    """chat.py passes ORM Message rows, not dicts (review Critical)."""
    from types import SimpleNamespace
    rows = [
        SimpleNamespace(role="assistant", content="回答"),
        SimpleNamespace(role="user", content="  第一条  "),
        SimpleNamespace(role="user", content="第二条"),
    ]
    assert _recent_user_texts(rows) == ["第二条", "第一条"]


def test_session_cache_key_includes_conversation_id():
    from app.services.memory_retrieval_service import _session_cache_key
    a = _session_cache_key("u1", ["继续"], "conv-a")
    b = _session_cache_key("u1", ["继续"], "conv-b")
    assert a != b


def test_to_dt_normalizes_aware_values():
    from app.services.memory_retrieval_service import _to_dt
    got = _to_dt("2026-10-17T17:30:42+00:00")
    assert got is not None and got.tzinfo is None
    assert (datetime(2026, 10, 17, 17, 30, 42) - got).total_seconds() == 0


def test_prefix_match_detects_own_message():
    recent = ["不对吧？我可以确定你把罗航中的一些事故和本次我说的事故搞混了。"]
    assert _matches_recent_user_text(
        "不对吧？我可以确定你把罗航中的一些事故和本次我说的事故搞混了。你仔细研判一下。",
        recent,
    )
    assert not _matches_recent_user_text("完全无关的另一件事", recent)


def test_guard_drops_text_match_even_when_old():
    recent = ["不对吧？我可以确定你把罗航中的一些事故和本次我说的事故搞混了。"]
    old = datetime.utcnow() - timedelta(days=30)
    cand = _cand("subconscious", recent[0] + " 补充", created=old)
    assert _self_reference_guard([cand], "subconscious", recent, 30) == []


def test_guard_drops_fresh_candidate_even_without_text_match():
    fresh = datetime.utcnow() - timedelta(minutes=5)
    cand = _cand("episodic", "另一段叙述", created=fresh, narrative="刚提取的事件")
    assert _self_reference_guard([cand], "episodic", [], 30) == []


def test_guard_drops_anything_created_after_conversation_start():
    """A long conversation must not ingest its own 3-hour-old events."""
    since = datetime.utcnow() - timedelta(hours=6)
    cand = _cand("episodic", "x", created=datetime.utcnow() - timedelta(hours=3),
                 narrative="本会话中途提取的事件")
    assert _self_reference_guard([cand], "episodic", [], 0, since_dt=since) == []
    old = _cand("episodic", "y", created=datetime.utcnow() - timedelta(hours=8),
                narrative="上一次会话的事件")
    assert _self_reference_guard([old], "episodic", [], 0, since_dt=since) == [old]


def test_guard_strips_role_prefix_before_text_match():
    recent = ["所以你能找到根源性的问题吗？vibeweaver 是不是还应该修改？"]
    cand = _cand("subconscious", "用户: 所以你能找到根源性的问题吗？vibeweaver 是不是还应该修改？",
                 created=datetime.utcnow() - timedelta(days=2))
    assert _self_reference_guard([cand], "subconscious", recent, 30) == []


def test_guard_keeps_old_unrelated_candidate():
    old = datetime.utcnow() - timedelta(days=7)
    cand = _cand("subconscious", "去年的一次部署记录", created=old)
    assert _self_reference_guard([cand], "subconscious", ["当前这句"], 30) == [cand]


def test_within_recent_minutes_parses_string_and_datetime():
    assert _within_recent_minutes(datetime.utcnow().isoformat(), 30)
    assert _within_recent_minutes(datetime.utcnow() - timedelta(days=2), 30) is False
    assert _within_recent_minutes(None, 30) is False


def test_v1_sections_have_boundary():
    from app.services.agent_service import _memory_injection_sections
    ctx = SimpleNamespace(
        memory_summary="长期摘要",
        dream_summary="梦境",
        memory_entries=[],
    )
    text = "\n\n".join(_memory_injection_sections(ctx))
    assert "记忆使用边界" in text
    assert "不得把其中的事件" in text
    assert "共享长期记忆" not in text
