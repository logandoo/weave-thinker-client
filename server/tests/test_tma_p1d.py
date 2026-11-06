"""P1d: artifact provenance, session-search precision, crypto action tokens."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import taint_service as taint


def test_notes_read_classifier_uses_artifact_taint():
    trusted = json.dumps({"notes": [
        {"id": "n1", "taint": "agent"},
        {"id": "n2", "taint": "trusted"},
    ]})
    assert taint.classify_tool_result(
        "notes", trusted, input_taint="trusted", tool_args={"action": "list_notes"},
    ) == "agent"

    untrusted = json.dumps({"notes": [
        {"id": "n1", "taint": "untrusted"},
        {"id": "n2", "taint": "agent"},
    ]})
    assert taint.classify_tool_result(
        "notes", untrusted, input_taint="trusted", tool_args={"action": "list_notes"},
    ) == "untrusted"

    # A legacy note with no taint annotation fails closed.
    missing = json.dumps({"notes": [{"id": "n1"}]})
    assert taint.classify_tool_result(
        "notes", missing, input_taint="trusted", tool_args={"action": "get_note"},
    ) == "untrusted"


def test_session_search_classifier_uses_result_taint():
    mixed = json.dumps({"results": [
        {"taint": "agent", "content": "a"},
        {"taint": "untrusted", "content": "b"},
    ]})
    assert taint.classify_tool_result(
        "session_search", mixed, input_taint="trusted",
    ) == "untrusted"

    clean = json.dumps({"results": [
        {"taint": "agent", "content": "a"},
        {"taint": "trusted", "content": "b"},
    ]})
    assert taint.classify_tool_result(
        "session_search", clean, input_taint="trusted",
    ) == "agent"

    # Missing taint annotation (legacy result) fails closed.
    missing = json.dumps({"results": [{"content": "a"}]})
    assert taint.classify_tool_result(
        "session_search", missing, input_taint="trusted",
    ) == "untrusted"


def test_artifact_model_and_migration():
    from app.db.database import ArtifactTaint

    columns = set(ArtifactTaint.__table__.columns.keys())
    assert {
        "user_id", "artifact_type", "artifact_id", "taint", "origin",
        "parent_ids",
    } <= columns
    source = Path(__file__).resolve().parents[1] / "app/db/migrations.py"
    text = source.read_text(encoding="utf-8")
    assert text.index("create_artifact_taint") < text.index('("pgvector_extension"')
    assert "artifact_taint_parent_ids_jsonb" in text
    from sqlalchemy.dialects.postgresql import JSONB
    assert isinstance(ArtifactTaint.__table__.columns.parent_ids.type, JSONB)


def test_action_token_payload_signature_and_expiry():
    from app.services import action_token_service as tokens

    now = int(time.time())
    payload = tokens._payload(
        "tok-1", "nonce-1", "tenant", "u1", "c1", "fp1", "terminal",
        now, now + 60,
    )
    payload["sig"] = tokens._sign(payload)
    assert tokens.verify_token_payload(payload, now=now)[0] is True

    tampered = dict(payload)
    tampered["action_fingerprint"] = "fp2"
    ok, reason = tokens.verify_token_payload(tampered, now=now)
    assert not ok and reason == "signature"

    ok, reason = tokens.verify_token_payload(payload, now=now + 61)
    assert not ok and reason == "expired"


def test_notes_write_unreadable_taint_fails_closed(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.tools import notes

    ledger = taint.get_or_create_ledger("c-notes", "u1")
    ledger.loaded = False
    captured = {}

    async def fake_load(_db, _cid, _uid):
        return ledger

    async def fake_record(_db, uid, atype, aid, value, **kwargs):
        captured["taint"] = value
        captured["origin"] = kwargs.get("origin")

    monkeypatch.setattr(taint, "load_ledger", fake_load)
    monkeypatch.setattr(taint, "record_artifact_taint", fake_record)
    asyncio.run(notes._record_note_taint(
        SimpleNamespace(), SimpleNamespace(id="u1"), "n1",
        {"conversation": SimpleNamespace(id="c-notes")},
    ))
    assert captured == {
        "taint": "untrusted", "origin": "untrusted_external",
    }


def test_provenance_off_rolls_back_classifiers(monkeypatch):
    from app.core.config import get_config

    monkeypatch.setitem(get_config().security, "tma_mode", "off")
    assert taint.classify_tool_result(
        "notes", json.dumps({"notes": [{"id": "n1"}]}),
        input_taint="trusted", tool_args={"action": "list_notes"},
    ) == "agent"
    assert taint.classify_tool_result(
        "session_search", json.dumps({"results": [{"content": "x"}]}),
        input_taint="trusted",
    ) == "agent"


def test_db_snapshot_verification_ignores_missing_witness():
    from app.services import verdict_log_service as verdicts

    payload = {
        "id": "v1", "tenant_id": "t", "user_id": "u1",
        "conversation_id": "c-db", "action_fingerprint": "fp",
        "tool_name": "terminal", "decision": "deny", "reason": "x",
        "taint": "untrusted", "corroborators": [], "mode": "enforce",
        "blocked": True, "created_at_epoch": 1,
    }
    entry = verdicts.next_entry("", 1, payload)
    checkpoint = verdicts.build_checkpoint(
        "t", 1, verdicts.merkle_root([entry["entry_hash"]]),
        entry["entry_hash"],
    )
    ok, reason = verdicts.verify_db_snapshot(
        [entry], head_hash=entry["entry_hash"], head_seq=1,
        checkpoints=[checkpoint],
    )
    assert ok, reason


def test_summary_provenance_inherits_conversation_taint(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.services import context_continuity_service as continuity

    ledger = taint.get_or_create_ledger("c-summary", "u1")
    ledger.user_id = "u1"
    ledger.mark("web:1", "untrusted", origin="untrusted_external")
    captured = {}

    async def fake_load(_db, _cid, _uid):
        return ledger

    async def fake_record(_db, uid, atype, aid, value, **kwargs):
        captured.update({
            "uid": uid, "type": atype, "id": aid,
            "taint": value, "origin": kwargs.get("origin"),
            "parents": kwargs.get("parent_ids"),
        })

    class FakeResult:
        def fetchone(self):
            return ("u1",)

    class FakeDB:
        async def execute(self, *_args, **_kwargs):
            return FakeResult()

    monkeypatch.setattr(taint, "load_ledger", fake_load)
    monkeypatch.setattr(taint, "record_artifact_taint", fake_record)
    asyncio.run(continuity._record_derived_provenance(
        FakeDB(), "c-summary", "summary",
    ))
    assert captured["taint"] == "untrusted"
    assert captured["origin"] == "untrusted_external"
    assert captured["parents"] == ["c-summary"]


def test_rebuild_anchor_refuses_broken_chain(monkeypatch, capsys):
    import asyncio
    from scripts import rebuild_tma_anchor as rebuild
    from app.services import verdict_log_service as verdicts

    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return False

    async def broken(_db, _tenant, require_nonempty=False, **_kwargs):
        assert require_nonempty is True
        return False, "bad"

    monkeypatch.setattr(verdicts, "verify_chain", broken)
    monkeypatch.setattr(rebuild, "AsyncSessionLocal", lambda: FakeSession())
    assert asyncio.run(rebuild._main("tenant-a", True)) == 1
    assert "refusing rebuild" in capsys.readouterr().err


def test_action_token_model_and_migration():
    from app.db.database import MemoryActionToken

    columns = set(MemoryActionToken.__table__.columns.keys())
    assert {
        "token_id", "user_id", "conversation_id", "action_fingerprint",
        "nonce", "expires_at_epoch", "used_at_epoch", "sig",
    } <= columns
    source = Path(__file__).resolve().parents[1] / "app/db/migrations.py"
    text = source.read_text(encoding="utf-8")
    assert text.index("create_memory_action_tokens") < text.index(
        '("pgvector_extension"'
    )
