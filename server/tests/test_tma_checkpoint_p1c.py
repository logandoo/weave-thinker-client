"""P1c1: Merkle checkpoints, external anchor, verdict-key rotation, retention."""
from __future__ import annotations

import asyncio
import json
import os
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


def _payload(n: int = 1):
    return {
        "id": f"id-{n}",
        "tenant_id": "tenant-a",
        "user_id": "u1",
        "conversation_id": "c1",
        "action_fingerprint": f"fp-{n}",
        "tool_name": "terminal",
        "decision": "deny",
        "reason": "test",
        "taint": "untrusted",
        "corroborators": [],
        "mode": "enforce",
        "blocked": True,
        "created_at_epoch": 1700000000 + n,
    }


def test_merkle_root_shape_and_sensitivity():
    a, b, c = "a" * 64, "b" * 64, "c" * 64
    assert v.merkle_root([a]) == a
    # Odd level duplicates its last node (3 and padded 4 agree).
    assert v.merkle_root([a, b, c]) == v.merkle_root([a, b, c, c])
    assert v.merkle_root([a, b]) != v.merkle_root([b, a])
    assert v.merkle_root([a, b, c]) != v.merkle_root([a, b, "d" * 64])
    assert len(v.merkle_root([])) == 64


def test_keyring_rotation_verifies_old_and_new(monkeypatch):
    _ring(monkeypatch, {"v1": "a" * 40, "v2": "b" * 40}, "v1")
    assert v.verdict_key_status() == "ready"
    first = v.next_entry("", 1, _payload(1))
    assert first["key_id"] == "v1"

    _ring(monkeypatch, {"v1": "a" * 40, "v2": "b" * 40}, "v2")
    second = v.next_entry(first["entry_hash"], 2, _payload(2))
    assert second["key_id"] == "v2"
    ok, broken = v.verify_records([first, second])
    assert ok, broken

    tampered = dict(second)
    tampered["key_id"] = "v9"
    ok, broken = v.verify_records([first, tampered])
    assert not ok and broken == "key:v9"


def test_overlong_key_id_falls_back_to_v1(monkeypatch):
    _ring(monkeypatch, {"v1": "a" * 40}, "k" * 20)
    assert v.verdict_key_id() == "v1"


def test_round3_explicit_false_checkpoint_verifies(monkeypatch):
    _ring(monkeypatch, {"v1": "a" * 40}, "v1")
    hashes = [r["entry_hash"] for r in _records()]
    body = {
        "tenant_id": "tenant-a",
        "seq": 3,
        "merkle_root": v.merkle_root(hashes),
        "last_hash": hashes[-1],
        "key_id": "v1",
        "created_at_epoch": 1700000000,
        "witness_required": False,
    }
    checkpoint = {**body, "sig": v.sign_payload(body)}
    ok, reason = v.verify_checkpoint(checkpoint)
    assert ok, reason


def test_checkpoint_verifies_after_rotation(monkeypatch):
    _ring(monkeypatch, {"v1": "a" * 40}, "v1")
    hashes = [r["entry_hash"] for r in _records()]
    checkpoint = v.build_checkpoint("tenant-a", 3, v.merkle_root(hashes), hashes[-1])
    assert checkpoint["key_id"] == "v1"
    _ring(monkeypatch, {"v1": "a" * 40, "v2": "b" * 40}, "v2")
    ok, reason = v.verify_checkpoint(checkpoint)
    assert ok, reason


def test_dev_key_suffix_resolves(monkeypatch):
    _ring(monkeypatch, {"v1": "dev-secret-change-in-production-000000"}, "v1")
    assert v.verdict_key_status() == "dev"
    entry = v.next_entry("", 1, _payload(1))
    assert entry["key_id"] == "v1-dev"
    assert v.verify_records([entry])[0] is True

    # Dev -> ready rotation keeps the old dev key addressable under v1-dev.
    old_dev = "dev-secret-change-in-production-000000"
    _ring(
        monkeypatch,
        {"v1": "b" * 40, "v1-dev": old_dev},
        "v1",
    )
    assert v.verdict_key_status() == "ready"
    assert v.verify_records([entry])[0] is True
    assert v.key_status_for_kids(["v1-dev", "v1"]) == "dev"
    assert v.key_status_for_kids(["v1"]) == "ready"


def _records():
    return [
        {"seq": 1, "entry_hash": "a" * 64},
        {"seq": 2, "entry_hash": "b" * 64},
        {"seq": 3, "entry_hash": "c" * 64},
    ]


def test_checkpoint_signature_and_prefix(monkeypatch):
    _ring(monkeypatch, {"v1": "a" * 40}, "v1")
    hashes = [r["entry_hash"] for r in _records()]
    checkpoint = v.build_checkpoint("tenant-a", 3, v.merkle_root(hashes), hashes[-1])
    assert v.verify_checkpoint(checkpoint)[0] is True
    assert v.verify_checkpoint_prefix(_records(), checkpoint)[0] is True

    bad_root = dict(checkpoint)
    bad_root["merkle_root"] = "d" * 64
    ok, reason = v.verify_checkpoint_prefix(_records(), bad_root)
    assert not ok and reason == "checkpoint:3"

    bad_sig = dict(checkpoint)
    bad_sig["last_hash"] = "e" * 64
    ok, reason = v.verify_checkpoint(bad_sig)
    assert not ok and reason == "checkpoint:3"


def test_checkpoint_anchor_stale_and_mismatch(monkeypatch):
    _ring(monkeypatch, {"v1": "a" * 40}, "v1")
    hashes = [r["entry_hash"] for r in _records()]
    checkpoint = v.build_checkpoint("tenant-a", 3, v.merkle_root(hashes), hashes[-1])
    old = v.build_checkpoint(
        "tenant-a", 2, v.merkle_root(hashes[:2]), hashes[1],
    )
    ok, reason = v.verify_checkpoint_prefix(_records(), checkpoint, anchor=old)
    assert not ok and reason == "anchor_stale"

    mismatch = v.build_checkpoint(
        "tenant-a", 3, "d" * 64, hashes[-1],
    )
    ok, reason = v.verify_checkpoint_prefix(_records(), checkpoint, anchor=mismatch)
    assert not ok and reason == "anchor_mismatch"

    newer = v.build_checkpoint(
        "tenant-a", 4, v.merkle_root(hashes + ["d" * 64]), "d" * 64,
    )
    ok, reason = v.verify_checkpoint_prefix(_records(), checkpoint, anchor=newer)
    assert not ok and reason == "checkpoint_missing"


def test_head_witness_never_downgrades(monkeypatch, tmp_path):
    _ring(monkeypatch, {"v1": "a" * 40}, "v1")
    monkeypatch.setenv("TMA_CHECKPOINT_DIR", str(tmp_path))
    v.write_head_witness("tenant-a", 5, "a" * 64)
    v.write_head_witness("tenant-a", 2, "b" * 64)
    assert v.read_head_witness("tenant-a")["seq"] == 5
    assert v.read_head_witness("tenant-a")["entry_hash"] == "a" * 64


def test_external_witness_copy_is_required_and_compared(monkeypatch, tmp_path):
    _ring(monkeypatch, {"v1": "a" * 40}, "v1")
    primary = tmp_path / "primary"
    copy = tmp_path / "copy"
    monkeypatch.setenv("TMA_CHECKPOINT_DIR", str(primary))
    monkeypatch.setenv("TMA_WITNESS_DIR", str(copy))
    records = _signed_chain(monkeypatch, 3)
    built = _anchor_chain(records, [3])
    v.write_anchor_chain("tenant-a", built["chain"])
    anchor = v.read_anchor("tenant-a")
    v.write_head_witness("tenant-a", 3, records[-1]["entry_hash"])
    assert v.read_anchor_copy("tenant-a") == anchor
    assert v.read_head_witness_copy("tenant-a") == v.read_head_witness("tenant-a")

    ok, reason = v.verify_snapshot(
        records, head_hash=records[-1]["entry_hash"], head_seq=3,
        checkpoints=[anchor["chain"][-1]], anchor=anchor,
        head_witness=v.read_head_witness("tenant-a"),
        anchor_copy=v.read_anchor_copy("tenant-a"),
        head_witness_copy=v.read_head_witness_copy("tenant-a"),
        witness_copy_required=True,
    )
    assert ok, reason

    v._witness_copy_path("anchor", "tenant-a").write_text(
        "{}", encoding="utf-8",
    )
    ok, reason = v.verify_snapshot(
        records, head_hash=records[-1]["entry_hash"], head_seq=3,
        checkpoints=[anchor["chain"][-1]], anchor=anchor,
        head_witness=v.read_head_witness("tenant-a"),
        anchor_copy=v.read_anchor_copy("tenant-a"),
        head_witness_copy=v.read_head_witness_copy("tenant-a"),
        witness_copy_required=True,
    )
    assert not ok and reason == "witness_copy_mismatch"


def test_head_witness_concurrent_writes_are_monotonic(monkeypatch, tmp_path):
    import threading

    _ring(monkeypatch, {"v1": "a" * 40}, "v1")
    monkeypatch.setenv("TMA_CHECKPOINT_DIR", str(tmp_path))
    threads = [
        threading.Thread(
            target=v.write_head_witness,
            args=("tenant-a", seq, f"{seq:064x}"),
        )
        for seq in range(1, 21)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    witness = v.read_head_witness("tenant-a")
    assert witness["seq"] == 20
    assert witness["entry_hash"] == f"{20:064x}"


def test_anchor_file_is_atomic_0600_and_not_downgraded(monkeypatch, tmp_path):
    _ring(monkeypatch, {"v1": "a" * 40}, "v1")
    monkeypatch.setenv("TMA_CHECKPOINT_DIR", str(tmp_path))
    hashes = [r["entry_hash"] for r in _records()]
    checkpoint = v.build_checkpoint("tenant-a", 3, v.merkle_root(hashes), hashes[-1])
    path = v.write_anchor(checkpoint)
    assert path.is_file()
    assert (os.stat(path).st_mode & 0o777) == 0o600
    assert v.read_anchor("tenant-a")["seq"] == 3

    old = v.build_checkpoint("tenant-a", 2, v.merkle_root(hashes[:2]), hashes[1])
    v.write_anchor(old)
    assert v.read_anchor("tenant-a")["seq"] == 3


def _signed_chain(monkeypatch, n):
    _ring(monkeypatch, {"v1": "a" * 40}, "v1")
    records = []
    prev = ""
    for seq in range(1, n + 1):
        entry = v.next_entry(prev, seq, _payload(seq))
        records.append(entry)
        prev = entry["entry_hash"]
    return records


def _anchor_chain(records, seqs):
    hashes = [r["entry_hash"] for r in records]
    chain = [
        v.build_checkpoint(
            "tenant-a", seq, v.merkle_root(hashes[:seq]), hashes[seq - 1],
        )
        for seq in seqs
    ]
    return {**chain[-1], "chain": chain}


def test_snapshot_clean_and_tail_above_checkpoint(monkeypatch):
    records = _signed_chain(monkeypatch, 3)
    anchor = _anchor_chain(records, [3])
    witness = v.build_head_witness("tenant-a", 3, records[-1]["entry_hash"])
    ok, reason = v.verify_snapshot(
        records, head_hash=records[-1]["entry_hash"], head_seq=3,
        checkpoints=[anchor["chain"][-1]], anchor=anchor, head_witness=witness,
    )
    assert ok, reason

    # A DB-only attacker deletes seq 3, rewinds the head, but the head witness
    # still says 3: the <=interval tail window is detected.
    ok, reason = v.verify_snapshot(
        records[:2], head_hash=records[1]["entry_hash"], head_seq=2,
        checkpoints=[], anchor=None, head_witness=witness,
    )
    assert not ok and reason == "truncated_after_checkpoint"


def test_snapshot_older_checkpoint_deletion_and_anchor_gaps(monkeypatch):
    records = _signed_chain(monkeypatch, 3)
    anchor = _anchor_chain(records, [2, 3])
    witness = v.build_head_witness("tenant-a", 3, records[-1]["entry_hash"])
    ok, reason = v.verify_snapshot(
        records, head_hash=records[-1]["entry_hash"], head_seq=3,
        checkpoints=[anchor["chain"][-1]], anchor=anchor, head_witness=witness,
    )
    assert not ok and reason == "checkpoint_missing"

    ok, reason = v.verify_snapshot(
        records, head_hash=records[-1]["entry_hash"], head_seq=3,
        checkpoints=[anchor["chain"][-1]], anchor=None, head_witness=witness,
    )
    assert not ok and reason == "anchor_missing"


def test_snapshot_head_witness_missing_stale_empty(monkeypatch):
    records = _signed_chain(monkeypatch, 3)
    anchor = _anchor_chain(records, [3])
    ok, reason = v.verify_snapshot(
        records, head_hash=records[-1]["entry_hash"], head_seq=3,
        checkpoints=[anchor["chain"][-1]], anchor=anchor, head_witness=None,
    )
    assert not ok and reason == "head_witness_missing"

    stale = v.build_head_witness("tenant-a", 2, records[1]["entry_hash"])
    ok, reason = v.verify_snapshot(
        records, head_hash=records[-1]["entry_hash"], head_seq=3,
        checkpoints=[], anchor=None, head_witness=stale,
    )
    assert not ok and reason == "head_witness_stale"

    ok, reason = v.verify_snapshot(
        [], head_hash="", head_seq=0, checkpoints=[],
        anchor=None, head_witness=stale, require_nonempty=True,
    )
    assert not ok and reason == "truncated_after_checkpoint"

    ok, reason = v.verify_snapshot(
        records, head_hash=records[-1]["entry_hash"], head_seq=3,
        checkpoints=[anchor["chain"][-1]], anchor=anchor, head_witness=None,
    )
    assert not ok and reason == "head_witness_missing"


def test_checkpoint_model_and_migration_before_pgvector():
    from app.db.database import MemoryVerdictCheckpoint

    columns = set(MemoryVerdictCheckpoint.__table__.columns.keys())
    assert {"tenant_id", "seq", "merkle_root", "last_hash", "sig", "key_id"} <= columns
    source = Path(__file__).resolve().parents[1] / "app/db/migrations.py"
    text = source.read_text(encoding="utf-8")
    assert text.index("create_memory_verdict_checkpoints") < text.index('("pgvector_extension"')


def test_config_checkpoint_interval(monkeypatch):
    security = get_config().security
    monkeypatch.setitem(security, "tma_checkpoint_interval", 32)
    assert get_config().tma_checkpoint_interval == 32
    monkeypatch.setitem(security, "tma_checkpoint_interval", -1)
    assert get_config().tma_checkpoint_interval == 0


def test_verify_verdict_chain_cli_reports_checkpoint(monkeypatch, capsys):
    from scripts import verify_verdict_chain as cli

    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return False

    async def fake_verify(_db, _tenant, require_nonempty=False):
        assert require_nonempty is True
        return True, ""

    async def fake_status(_db, _tenant):
        return "ready"

    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(cli, "verify_chain", fake_verify)
    monkeypatch.setattr(cli, "chain_key_status", fake_status)
    monkeypatch.setattr(cli, "read_anchor", lambda _tenant: {"seq": 3})
    assert asyncio.run(cli._main("tenant-a")) == 0
    out = capsys.readouterr().out
    assert "checkpoint=3" in out
    assert "key_status=ready" in out


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeDB:
    """Minimal async session for the two checkpoint SQL shapes."""

    def __init__(self, tenant, entries, head, checkpoints=None):
        self.tenant = tenant
        self.entries = entries
        self.head = head
        self.checkpoints = list(checkpoints or [])

    def _record_rows(self):
        return [(
            e["id"], e["tenant_id"], e["seq"], e["user_id"],
            e["conversation_id"], e["action_fingerprint"], e["tool_name"],
            e["decision"], e["reason"], e["taint"],
            json.dumps(e["corroborators"]), e["mode"], e["blocked"],
            e["created_at_epoch"], e["prev_hash"], e["entry_hash"],
            e["sig"], e["key_id"],
        ) for e in self.entries]

    def _checkpoint_rows(self):
        return [(
            c["tenant_id"], c["seq"], c["merkle_root"], c["last_hash"],
            c["sig"], c["key_id"], bool(c.get("witness_required")),
            c["created_at_epoch"],
        ) for c in self.checkpoints]

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        if "FROM memory_verdict_heads" in sql:
            return _FakeResult([self.head] if self.head else [])
        if "FROM memory_verdicts" in sql:
            if "seq, entry_hash" in sql:
                target = int((params or {}).get("target") or 0)
                return _FakeResult([
                    (e["seq"], e["entry_hash"]) for e in self.entries
                    if e["seq"] <= target
                ])
            return _FakeResult(self._record_rows())
        if "FROM memory_verdict_checkpoints" in sql:
            if "AND seq = :target" in sql:
                target = int((params or {}).get("target") or 0)
                return _FakeResult([
                    row for row in self._checkpoint_rows() if row[1] == target
                ])
            return _FakeResult(sorted(
                self._checkpoint_rows(), key=lambda row: row[1],
            ))
        if "INSERT INTO memory_verdict_checkpoints" in sql:
            data = dict(params or {})
            if not any(c["seq"] == data["seq"] for c in self.checkpoints):
                self.checkpoints.append(data)
            return _FakeResult([])
        return _FakeResult([])

    async def commit(self):
        return None


def test_write_checkpoint_and_verify_chain_hermetic(monkeypatch, tmp_path):
    records = _signed_chain(monkeypatch, 2)
    monkeypatch.setenv("TMA_CHECKPOINT_DIR", str(tmp_path))
    db = _FakeDB(
        "tenant-a", records,
        (records[-1]["entry_hash"], 2),
    )
    first = asyncio.run(v.write_checkpoint(db, "tenant-a", seq=2))
    second = asyncio.run(v.write_checkpoint(db, "tenant-a", seq=2))
    assert first and second
    assert first["sig"] == second["sig"]
    assert len(db.checkpoints) == 1
    assert len(v.read_anchor("tenant-a")["chain"]) == 1

    ok, reason = asyncio.run(v.verify_chain(db, "tenant-a", require_nonempty=True))
    assert ok, reason

    # Delete the DB checkpoint row; the anchor chain still has it.
    db.checkpoints.clear()
    ok, reason = asyncio.run(v.verify_chain(db, "tenant-a", require_nonempty=True))
    assert not ok and reason == "checkpoint_missing"

    # A later checkpoint must NOT heal (shrink) the anchor after an older
    # checkpoint row is deleted.
    db2 = _FakeDB("tenant-b", records, (records[-1]["entry_hash"], 2))
    asyncio.run(v.write_checkpoint(db2, "tenant-b", seq=1))
    asyncio.run(v.write_checkpoint(db2, "tenant-b", seq=2))
    assert [c["seq"] for c in v.read_anchor("tenant-b")["chain"]] == [1, 2]
    db2.checkpoints = [c for c in db2.checkpoints if c["seq"] != 1]
    healed = asyncio.run(v.write_checkpoint(db2, "tenant-b", seq=2))
    assert healed and healed.get("anchor_written") is False
    assert [c["seq"] for c in v.read_anchor("tenant-b")["chain"]] == [1, 2]
    ok, reason = asyncio.run(v.verify_chain(db2, "tenant-b", require_nonempty=True))
    assert not ok and reason == "checkpoint_missing"


def test_prune_tma_audit_only_targets_taint():
    from scripts import prune_tma_audit

    sql = prune_tma_audit.build_delete_sql()
    assert "DELETE FROM taint_states" in sql
    assert "max_taint = 'trusted'" in sql
    assert "memory_verdicts" not in sql
    assert "memory_verdict_checkpoints" not in sql
