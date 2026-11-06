"""P2: verdict retention/redaction (hash-only tombstones + signed boundary).

RED-first spec for the P2 wave. The chain stays tamper-evident:
pruned rows are hash-only tombstones, verified by linkage + signed Merkle
checkpoints, and a signed retention boundary prevents prefix re-growth.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.core.config import get_config
from app.services import verdict_log_service as v


def _ring(monkeypatch, mapping, kid):
    security = get_config().security
    monkeypatch.setitem(security, "tma_verdict_keyring", mapping)
    monkeypatch.setitem(security, "tma_verdict_key_id", kid)
    monkeypatch.setitem(security, "tma_verdict_key", "")
    monkeypatch.setitem(security, "memory_hmac_key", "")


def _payload(n: int, **overrides):
    base = {
        "id": f"id-{n}",
        "tenant_id": "tenant-a",
        "user_id": "u1",
        "conversation_id": "c1",
        "action_fingerprint": f"fp-{n}",
        "tool_name": "terminal",
        "decision": "allow",
        "reason": "canonical ask",
        "taint": "agent",
        "corroborators": [],
        "mode": "shadow",
        "blocked": False,
        "created_at_epoch": 1700000000 + n,
    }
    base.update(overrides)
    return base


def _chain(n: int = 4):
    entries = []
    prev = ""
    for i in range(1, n + 1):
        entry = v.next_entry(prev, i, _payload(i))
        entries.append(entry)
        prev = entry["entry_hash"]
    return entries


# ---------------------------------------------------------------- redaction

def test_redact_verdict_reason_masks_secrets():
    api_key = "abc" + "123456789"
    gh_token = "ghp_" + "abcdefgh12345678"
    sk_token = "sk-" + "proj-" + "abc" + "def123456"
    password = "hunter" + "2"
    url_pw = "pw" + "123"
    digest = "a1b2c3d4" * 8
    raw = (
        "call with api_key=" + api_key + " and Authorization: Bearer " + gh_token
        + " " + sk_token + " password=" + password
        + " https://bob:" + url_pw + "@example.com/x digest " + digest
    )
    out = v.redact_verdict_reason(raw)
    assert api_key not in out
    assert gh_token not in out
    assert sk_token not in out
    assert password not in out
    assert url_pw not in out
    assert digest not in out
    assert "[redacted]" in out
    # JSON-quoted and prefixed key names are covered too.
    pw = "hunter" + "2"
    client_secret = "super" + "-secret-" + "value"
    db_pw = "hunter" + "2"
    auth_token = "abcdef" + "123456"
    for raw in (
        '{"password": "' + pw + '!"}',
        "client_secret=" + client_secret,
        "db_password=" + db_pw,
        "auth_token=" + auth_token,
        "AWS_SECRET_ACCESS_KEY=abcdef",
        "password: 'abc123'",
    ):
        cleaned = v.redact_verdict_reason(raw)
        assert pw not in cleaned
        assert client_secret not in cleaned
        assert db_pw not in cleaned
        assert auth_token not in cleaned
        assert "[redacted]" in cleaned
    # Benign prose is preserved.
    assert v.redact_verdict_reason("user asked to list /tmp files") == (
        "user asked to list /tmp files"
    )


def test_redact_verdict_reason_bounds_length():
    out = v.redact_verdict_reason("x" * 5000, limit=100)
    assert len(out) <= 100
    assert out.endswith("…")


def test_build_verdict_payload_redacts_before_signing(monkeypatch):
    security = get_config().security
    monkeypatch.setitem(security, "verdict_reason_redaction", True)
    secret = "super" + "secret" + "value"
    payload = v.build_verdict_payload(
        tenant_id="t", user_id="u", conversation_id="c",
        action_fingerprint="fp", tool_name="terminal", decision="allow",
        reason="token=" + secret, taint="agent", mode="shadow",
    )
    assert secret not in payload["reason"]
    # And the stored entry signature verifies over the redacted payload only.
    entry = v.next_entry("", 1, payload)
    ok, broken = v.verify_records([entry])
    assert ok, broken


# ----------------------------------------------------- retention record (M4)

def test_retention_record_is_signed_and_tamper_evident(monkeypatch):
    _ring(monkeypatch, {"v1": "a" * 40}, "v1")
    record = v.build_retention_record(
        "tenant-a", 2, "b" * 64, "c" * 64, secret=b"a" * 40,
        updated_at_epoch=1700000000,
    )
    ok, reason = v.verify_retention_record(record, secret=b"a" * 40)
    assert ok, reason
    for field, value in (
        ("pruned_through_seq", 3),
        ("boundary_hash", "d" * 64),
        ("boundary_prev_hash", "e" * 64),
        ("tenant_id", "tenant-b"),
    ):
        tampered = dict(record)
        tampered[field] = value
        ok, _ = v.verify_retention_record(tampered, secret=b"a" * 40)
        assert not ok, field
    tampered = dict(record)
    tampered["sig"] = "0" * 64
    assert not v.verify_retention_record(tampered, secret=b"a" * 40)[0]


def test_retention_boundary_is_monotonic(monkeypatch):
    _ring(monkeypatch, {"v1": "a" * 40}, "v1")
    first = v.build_retention_record("t", 2, "b" * 64, "c" * 64)
    assert v.check_retention_forward(None, first)[0]
    later = v.build_retention_record("t", 4, "d" * 64, "e" * 64)
    assert v.check_retention_forward(first, later)[0]
    # Equal boundary with a different hash is a rewrite, not an extension.
    same_seq_other = v.build_retention_record("t", 2, "f" * 64, "c" * 64)
    assert not v.check_retention_forward(first, same_seq_other)[0]
    # Equal boundary + identical hashes is idempotent.
    same = v.build_retention_record("t", 2, "b" * 64, "c" * 64)
    assert v.check_retention_forward(first, same)[0]
    # Shrinking the pruned prefix is rejected.
    assert not v.check_retention_forward(later, first)[0]


# ------------------------------------------- pruned rows + verification core

def test_verify_records_uses_retention_boundary():
    entries = _chain(5)
    pruned = [
        {**{k: e[k] for k in ("seq", "prev_hash", "entry_hash", "key_id")}, "pruned": True}
        for e in entries[:2]
    ]
    live = entries[2:]
    # Merged chain (tombstones + live) verifies from seq 1.
    ok, broken = v.verify_records(
        pruned + live,
        head_hash=entries[-1]["entry_hash"],
        head_seq=5,
    )
    assert ok, broken
    # A live-only chain that starts after the boundary is verifiable when the
    # caller supplies the boundary explicitly.
    ok, broken = v.verify_records(
        live,
        start_seq=2,
        start_prev_hash=entries[1]["entry_hash"],
        head_hash=entries[-1]["entry_hash"],
        head_seq=5,
    )
    assert ok, broken
    # Live-only without the boundary is a detected gap.
    ok, _ = v.verify_records(live, head_hash=entries[-1]["entry_hash"], head_seq=5)
    assert not ok


def test_pruned_rows_are_linkage_checked():
    entries = _chain(4)
    pruned = [
        {**{k: e[k] for k in ("seq", "prev_hash", "entry_hash", "key_id")}, "pruned": True}
        for e in entries[:2]
    ]
    # A missing pruned row creates a seq gap.
    ok, _ = v.verify_records(
        [pruned[1]] + entries[2:], start_seq=2,
        start_prev_hash=entries[1]["entry_hash"],
    )
    assert not ok
    # Rewriting a pruned entry hash breaks the next row's prev_hash.
    rewritten = [dict(pruned[0]), dict(pruned[1])]
    rewritten[1]["entry_hash"] = "d" * 64
    ok, _ = v.verify_records(
        rewritten + entries[2:], start_seq=2,
        start_prev_hash=entries[1]["entry_hash"],
    )
    assert not ok


def test_verify_db_snapshot_with_tombstones_and_checkpoint(monkeypatch):
    _ring(monkeypatch, {"v1": "a" * 40}, "v1")
    entries = _chain(4)
    checkpoint = v.build_checkpoint(
        "t", 4, v.merkle_root([e["entry_hash"] for e in entries]),
        entries[-1]["entry_hash"],
    )
    tombstones = [
        {**{k: e[k] for k in ("seq", "prev_hash", "entry_hash", "key_id")}, "pruned": True}
        for e in entries[:2]
    ]
    retention = v.build_retention_record(
        "t", 2, entries[1]["entry_hash"], entries[1]["prev_hash"],
    )
    ok, reason = v.verify_db_snapshot(
        tombstones + entries[2:],
        head_hash=entries[-1]["entry_hash"],
        head_seq=4,
        checkpoints=[checkpoint],
        retention=retention,
    )
    assert ok, reason
    # Boundary must match the tombstone at that seq.
    bad_retention = v.build_retention_record("t", 2, "f" * 64, entries[1]["prev_hash"])
    ok, reason = v.verify_db_snapshot(
        tombstones + entries[2:],
        head_hash=entries[-1]["entry_hash"],
        head_seq=4,
        checkpoints=[checkpoint],
        retention=bad_retention,
    )
    assert not ok
    assert "retention" in reason
    # A rewritten tombstone breaks the checkpoint Merkle root.
    broken = [dict(t) for t in tombstones]
    broken[0]["entry_hash"] = "d" * 64
    ok, _ = v.verify_db_snapshot(
        broken + entries[2:],
        head_hash=entries[-1]["entry_hash"],
        head_seq=4,
        checkpoints=[checkpoint],
        retention=retention,
    )
    assert not ok


def test_head_witness_binds_retention_boundary(monkeypatch):
    _ring(monkeypatch, {"v1": "a" * 40}, "v1")
    entries = _chain(3)
    tombstones = [
        {**{k: e[k] for k in ("seq", "prev_hash", "entry_hash", "key_id")}, "pruned": True}
        for e in entries[:2]
    ]
    retention = v.build_retention_record(
        "t", 2, entries[1]["entry_hash"], entries[1]["prev_hash"],
    )
    witness = v.build_head_witness(
        "t", 3, entries[-1]["entry_hash"], retention_seq=2,
    )
    ok, reason = v.verify_head_witness(witness)
    assert ok, reason
    # A witness without the marker cannot anchor a retained chain.
    unanchored = v.build_head_witness("t", 3, entries[-1]["entry_hash"])
    ok, reason = v.verify_snapshot(
        tombstones + entries[2:],
        head_hash=entries[-1]["entry_hash"],
        head_seq=3,
        checkpoints=[],
        anchor=None,
        head_witness=unanchored,
        retention=retention,
    )
    assert not ok
    assert reason == "retention_unanchored"
    # A surgical restore that removes the retention row + tombstones but keeps
    # the witness marker is still detected.
    ok, reason = v.verify_snapshot(
        entries,
        head_hash=entries[-1]["entry_hash"],
        head_seq=3,
        checkpoints=[],
        anchor=None,
        head_witness=witness,
        retention=None,
    )
    assert not ok
    assert reason == "retention_missing"


# ------------------------------------------------------------- prune planning

def _rows():
    return [
        {"seq": 1, "decision": "allow", "blocked": False, "created_at_epoch": 10},
        {"seq": 2, "decision": "allow", "blocked": False, "created_at_epoch": 20},
        {"seq": 3, "decision": "allow", "blocked": False, "created_at_epoch": 30},
        {"seq": 4, "decision": "deny", "blocked": True, "created_at_epoch": 40},
        {"seq": 5, "decision": "allow", "blocked": False, "created_at_epoch": 50},
    ]


def test_plan_retention_stops_at_deny_and_respects_checkpoint():
    plan = v.plan_retention(
        _rows(), pruned_through_seq=0, checkpoint_seq=4, cutoff_epoch=100,
    )
    assert [r["seq"] for r in plan["selected"]] == [1, 2, 3]
    assert plan["pruned_through_seq"] == 3
    assert plan["stop"] == "deny_kept"

    plan = v.plan_retention(
        _rows(), pruned_through_seq=0, checkpoint_seq=2, cutoff_epoch=100,
    )
    assert [r["seq"] for r in plan["selected"]] == [1, 2]
    assert plan["stop"] == "checkpoint"


def test_plan_retention_stops_at_recent_row_and_continues_from_boundary():
    rows = _rows()
    plan = v.plan_retention(
        rows, pruned_through_seq=0, checkpoint_seq=5, cutoff_epoch=25,
    )
    # Rows 1-2 are old enough; row 3 is too new.
    assert [r["seq"] for r in plan["selected"]] == [1, 2]
    assert plan["stop"] == "too_new"

    plan = v.plan_retention(
        rows, pruned_through_seq=2, checkpoint_seq=3, cutoff_epoch=100,
    )
    assert [r["seq"] for r in plan["selected"]] == [3]


def test_plan_retention_refuses_unknown_age_rows():
    rows = [
        {"seq": 1, "decision": "allow", "blocked": False, "created_at_epoch": 0},
    ]
    plan = v.plan_retention(rows, pruned_through_seq=0, checkpoint_seq=1, cutoff_epoch=100)
    assert plan["selected"] == []
    assert plan["stop"] == "unknown_age"


def test_plan_retention_refuses_non_contiguous_prefix():
    plan = v.plan_retention(
        _rows()[2:], pruned_through_seq=0, checkpoint_seq=5, cutoff_epoch=100,
    )
    assert plan["selected"] == []
    assert plan["pruned_through_seq"] == 0
    assert plan["stop"] == "gap"


def test_tombstone_without_retention_is_rejected():
    entries = _chain(4)
    tombstones = [
        {**{k: e[k] for k in ("seq", "prev_hash", "entry_hash", "key_id")}, "pruned": True}
        for e in entries[:2]
    ]
    checkpoint = v.build_checkpoint(
        "t", 4, v.merkle_root([e["entry_hash"] for e in entries]),
        entries[-1]["entry_hash"],
    )
    ok, reason = v.verify_db_snapshot(
        tombstones + entries[2:],
        head_hash=entries[-1]["entry_hash"],
        head_seq=4,
        checkpoints=[checkpoint],
        retention=None,
    )
    assert not ok
    assert reason == "unsigned_pruned"


def test_tombstone_above_the_boundary_is_rejected():
    entries = _chain(5)
    tombstones = [
        {**{k: e[k] for k in ("seq", "prev_hash", "entry_hash", "key_id")}, "pruned": True}
        for e in entries[:3]
    ]
    # Signed boundary only covers 1..2: the tombstone at 3 must be rejected.
    retention = v.build_retention_record(
        "t", 2, entries[1]["entry_hash"], entries[1]["prev_hash"],
    )
    ok, reason = v.verify_db_snapshot(
        tombstones + entries[3:],
        head_hash=entries[-1]["entry_hash"],
        head_seq=5,
        checkpoints=[],
        retention=retention,
    )
    assert not ok
    assert reason == "retention_mismatch"


def test_retention_deletion_or_replay_is_detected():
    entries = _chain(4)
    tombstones = [
        {**{k: e[k] for k in ("seq", "prev_hash", "entry_hash", "key_id")}, "pruned": True}
        for e in entries[:3]
    ]
    # Deleted retention row: unsigned tombstones are never trusted.
    ok, reason = v.verify_db_snapshot(
        tombstones + entries[3:],
        head_hash=entries[-1]["entry_hash"],
        head_seq=4,
        checkpoints=[],
        retention=None,
    )
    assert not ok and reason == "unsigned_pruned"
    # Replayed older boundary (through 2) cannot cover tombstones 1..3.
    replayed = v.build_retention_record(
        "t", 2, entries[1]["entry_hash"], entries[1]["prev_hash"],
    )
    ok, reason = v.verify_db_snapshot(
        tombstones + entries[3:],
        head_hash=entries[-1]["entry_hash"],
        head_seq=4,
        checkpoints=[],
        retention=replayed,
    )
    assert not ok and reason == "retention_mismatch"


# ------------------------------------------------------- schema + SQL + flags

def test_retention_models_and_migrations_exist():
    from app.db.database import MemoryVerdictRedacted, MemoryVerdictRetention

    redacted_cols = set(MemoryVerdictRedacted.__table__.columns.keys())
    assert redacted_cols == {"tenant_id", "seq", "prev_hash", "entry_hash", "key_id"}
    retention_cols = set(MemoryVerdictRetention.__table__.columns.keys())
    assert {
        "tenant_id", "pruned_through_seq", "boundary_hash",
        "boundary_prev_hash", "sig", "key_id", "updated_at_epoch",
    } <= retention_cols

    source = (Path(__file__).resolve().parents[1] / "app/db/migrations.py").read_text()
    assert "memory_verdict_redacted" in source
    assert "memory_verdict_retention" in source


def test_prune_sql_is_prefix_bounded_and_keeps_deny():
    from scripts import prune_tma_audit as prune

    tombstone_sql = prune.build_tombstone_sql()
    delete_sql = prune.build_delete_verdicts_sql()
    assert "memory_verdicts" in tombstone_sql
    assert "seq <= :through" in tombstone_sql
    assert "decision <> 'deny'" in tombstone_sql
    assert "blocked = FALSE" in tombstone_sql
    assert "seq <= :through" in delete_sql
    assert "decision <> 'deny'" in delete_sql
    assert "blocked = FALSE" in delete_sql
    assert "memory_verdict_retention" in prune.build_retention_upsert_sql()
    assert "EXCLUDED.pruned_through_seq" in prune.build_retention_upsert_sql()


def test_token_prune_sql_targets_used_or_expired_only():
    sql = __import__("scripts.prune_tma_audit", fromlist=["build_token_delete_sql"]).build_token_delete_sql()
    assert "memory_action_tokens" in sql
    assert "used_at_epoch" in sql
    assert "expires_at_epoch" in sql


# -------------------------------------------- default single-tenant behaviour

def test_default_tenant_paths_are_unchanged(monkeypatch):
    from app.services import tenant_service

    monkeypatch.delenv("WT_TENANT_ID", raising=False)
    monkeypatch.delenv("WT_MULTI_TENANT_EXPERIMENTAL", raising=False)
    monkeypatch.setitem(get_config().security, "tenant_id", "")
    monkeypatch.setitem(get_config().security, "multi_tenant_experimental", False)
    assert tenant_service.tenant_segment() == ""
    assert tenant_service.scoped_root(Path("/tmp/base")) == Path("/tmp/base")
    assert tenant_service.tenant_config_errors() == []
