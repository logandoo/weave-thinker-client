"""TMA-NM retention CLI: taint states, verdict payloads, action tokens.

Dry-run by default. Four independent retention targets:

* default (no flags) — old fully-trusted ``taint_states`` rows.
* ``--verdicts`` — P2: move a **checkpoint-covered, contiguous, older-than-cutoff
  prefix** of ``memory_verdicts`` into hash-only ``memory_verdict_redacted``
  tombstones and record a signed ``memory_verdict_retention`` boundary.  Deny /
  blocked verdicts, unknown-age legacy rows (``created_at_epoch = 0``) and
  anything newer than ``--days`` are never pruned.  The chain stays verifiable:
  tombstones keep seq/prev_hash/entry_hash, and the signed Merkle checkpoints
  still cover the pruned prefix.  ``--execute`` requires the full external
  witness check (a DB rollback cannot be laundered into a new boundary).
* ``--tokens`` — used or expired ``memory_action_tokens`` rows.
* ``--quarantine`` — old ``injection_quarantine`` telemetry rows.

``memory_verdict_checkpoints`` and the anchor/witness files are never touched.

Usage (from backend/):
    python -m scripts.prune_tma_audit --days 90
    python -m scripts.prune_tma_audit --verdicts --days 90
    python -m scripts.prune_tma_audit --verdicts --tokens --quarantine --days 90 --execute
"""
from __future__ import annotations

import argparse
import asyncio
import time

from sqlalchemy import text

from app.db.database import AsyncSessionLocal


def build_delete_sql() -> str:
    # Only fully-trusted conversations may be forgotten.  Deleting an
    # untrusted/agent row would make a resumed conversation default to
    # trusted (a security downgrade).
    return (
        "DELETE FROM taint_states "
        "WHERE max_taint = 'trusted' "
        "AND updated_at < NOW() - (:days * INTERVAL '1 day')"
    )


def build_count_sql() -> str:
    return (
        "SELECT COUNT(*) FROM taint_states "
        "WHERE max_taint = 'trusted' "
        "AND updated_at < NOW() - (:days * INTERVAL '1 day')"
    )


def build_tombstone_sql() -> str:
    return (
        "INSERT INTO memory_verdict_redacted "
        "(tenant_id, seq, prev_hash, entry_hash, key_id) "
        "SELECT tenant_id, seq, prev_hash, entry_hash, key_id "
        "FROM memory_verdicts "
        "WHERE tenant_id = :tenant AND seq <= :through "
        "AND decision <> 'deny' AND blocked = FALSE "
        "ON CONFLICT (tenant_id, seq) DO NOTHING"
    )


def build_delete_verdicts_sql() -> str:
    return (
        "DELETE FROM memory_verdicts "
        "WHERE tenant_id = :tenant AND seq <= :through "
        "AND decision <> 'deny' AND blocked = FALSE"
    )


def build_retention_upsert_sql() -> str:
    # Monotonic: a concurrent/stale writer can never move the boundary back.
    return (
        "INSERT INTO memory_verdict_retention "
        "(tenant_id, pruned_through_seq, boundary_hash, boundary_prev_hash, "
        " sig, key_id, updated_at_epoch, updated_at) "
        "VALUES (:tenant, :through, :boundary_hash, :boundary_prev_hash, "
        "        :sig, :key_id, :updated_at_epoch, NOW()) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "  pruned_through_seq = EXCLUDED.pruned_through_seq, "
        "  boundary_hash = EXCLUDED.boundary_hash, "
        "  boundary_prev_hash = EXCLUDED.boundary_prev_hash, "
        "  sig = EXCLUDED.sig, "
        "  key_id = EXCLUDED.key_id, "
        "  updated_at_epoch = EXCLUDED.updated_at_epoch, "
        "  updated_at = NOW() "
        "WHERE memory_verdict_retention.pruned_through_seq "
        "      <= EXCLUDED.pruned_through_seq"
    )


def build_token_delete_sql() -> str:
    return (
        "DELETE FROM memory_action_tokens "
        "WHERE issued_at_epoch < :cutoff "
        "AND (used_at_epoch > 0 OR expires_at_epoch < :now)"
    )


def build_token_count_sql() -> str:
    return (
        "SELECT COUNT(*) FROM memory_action_tokens "
        "WHERE issued_at_epoch < :cutoff "
        "AND (used_at_epoch > 0 OR expires_at_epoch < :now)"
    )


def build_quarantine_delete_sql() -> str:
    return "DELETE FROM injection_quarantine WHERE created_at_epoch < :cutoff"


def build_quarantine_count_sql() -> str:
    return "SELECT COUNT(*) FROM injection_quarantine WHERE created_at_epoch < :cutoff"


async def _read_retention(db, tenant: str) -> dict | None:
    row = (await db.execute(
        text(
            "SELECT tenant_id, pruned_through_seq, boundary_hash, "
            "boundary_prev_hash, key_id, updated_at_epoch, sig "
            "FROM memory_verdict_retention WHERE tenant_id = :tenant"
        ),
        {"tenant": tenant},
    )).fetchone()
    if row is None:
        return None
    return {
        "tenant_id": str(row[0] or ""),
        "pruned_through_seq": int(row[1] or 0),
        "boundary_hash": str(row[2] or ""),
        "boundary_prev_hash": str(row[3] or ""),
        "key_id": str(row[4] or ""),
        "updated_at_epoch": int(row[5] or 0),
        "sig": str(row[6] or ""),
    }


async def _prune_verdicts(db, tenant: str, cutoff: int, execute: bool) -> int:
    """Return pruned row count, or -1 when the tenant was refused."""
    from app.services import verdict_log_service as v

    # Destructive mode must see the external witnesses too: a DB restored to an
    # earlier point must not be laundered into a fresh signed boundary.
    ok, reason = await v.verify_chain(
        db, tenant, allow_missing_witness=not execute,
    )
    if not ok:
        print(f"tenant={tenant!r}: refusing to prune a broken chain ({reason})")
        return -1
    checkpoint_seq = int((await db.execute(
        text(
            "SELECT COALESCE(MAX(seq), 0) FROM memory_verdict_checkpoints "
            "WHERE tenant_id = :tenant"
        ),
        {"tenant": tenant},
    )).scalar() or 0)
    if execute:
        # Serialise concurrent prune runs per tenant for the whole transaction.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:tenant))"),
            {"tenant": tenant},
        )
    previous = await _read_retention(db, tenant)
    rows = [
        {
            "seq": int(r[0] or 0),
            "decision": str(r[1] or ""),
            "blocked": bool(r[2]),
            "created_at_epoch": int(r[3] or 0),
            "entry_hash": str(r[4] or ""),
            "prev_hash": str(r[5] or ""),
        }
        for r in (await db.execute(
            text(
                "SELECT seq, decision, blocked, created_at_epoch, "
                "entry_hash, prev_hash FROM memory_verdicts "
                "WHERE tenant_id = :tenant ORDER BY seq ASC"
            ),
            {"tenant": tenant},
        )).fetchall()
    ]
    plan = v.plan_retention(
        rows,
        pruned_through_seq=int((previous or {}).get("pruned_through_seq") or 0),
        checkpoint_seq=checkpoint_seq,
        cutoff_epoch=cutoff,
    )
    count = len(plan["selected"])
    if not count:
        print(
            f"tenant={tenant!r}: nothing prunable "
            f"(stop={plan['stop']}, checkpoint_seq={checkpoint_seq})"
        )
        return 0
    if not execute:
        print(
            f"dry-run tenant={tenant!r}: {count} verdict(s) seq "
            f"{plan['selected'][0]['seq']}..{plan['pruned_through_seq']} "
            f"-> tombstones (stop={plan['stop']})"
        )
        return count

    record = v.build_retention_record(
        tenant,
        plan["pruned_through_seq"],
        plan["boundary_hash"],
        plan["boundary_prev_hash"],
    )
    ok, reason = v.check_retention_forward(previous, record)
    if not ok:
        print(f"tenant={tenant!r}: retention refused ({reason})")
        return -1
    params = {"tenant": tenant, "through": plan["pruned_through_seq"]}
    await db.execute(text(build_tombstone_sql()), params)
    updated = await db.execute(text(build_retention_upsert_sql()), {
        "tenant": tenant,
        "through": record["pruned_through_seq"],
        "boundary_hash": record["boundary_hash"],
        "boundary_prev_hash": record["boundary_prev_hash"],
        "sig": record["sig"],
        "key_id": record["key_id"],
        "updated_at_epoch": record["updated_at_epoch"],
    })
    if int(updated.rowcount or 0) != 1:
        await db.rollback()
        print(f"tenant={tenant!r}: retention boundary moved concurrently")
        return -1
    await db.execute(text(build_delete_verdicts_sql()), params)
    await db.commit()
    # Anchor the signed boundary in the head witness (second medium when
    # configured).  Without this a surgical DB restore could remove the
    # retention row + tombstones and still verify green.
    head = (await db.execute(
        text(
            "SELECT last_hash, last_seq FROM memory_verdict_heads "
            "WHERE tenant_id = :tenant"
        ),
        {"tenant": tenant},
    )).fetchone()
    if head is not None:
        try:
            v.write_head_witness(
                tenant, int(head[1] or 0), str(head[0] or ""),
                retention_seq=record["pruned_through_seq"],
            )
        except Exception:
            print(
                f"tenant={tenant!r}: pruned but the head witness update failed; "
                "verification will fail closed until it is rebuilt "
                "(scripts.rebuild_tma_anchor)",
            )
            return -1
    print(
        f"pruned tenant={tenant!r}: {count} verdict(s) seq "
        f"{plan['selected'][0]['seq']}..{plan['pruned_through_seq']} "
        f"(stop={plan['stop']})"
    )
    return count


async def _run(
    days: int,
    execute: bool,
    *,
    verdicts: bool = False,
    tokens: bool = False,
    quarantine: bool = False,
) -> int:
    failures = 0
    async with AsyncSessionLocal() as db:
        if not (verdicts or tokens or quarantine):
            if execute:
                result = await db.execute(text(build_delete_sql()), {"days": days})
                await db.commit()
                count = int(result.rowcount or 0)
                print(f"pruned taint_states: {count} row(s) older than {days}d")
            else:
                result = await db.execute(text(build_count_sql()), {"days": days})
                count = int(result.scalar() or 0)
                print(
                    f"dry-run: {count} taint_states row(s) older than {days}d "
                    "(verdicts/checkpoints/anchor are never touched)"
                )
            return 0
        cutoff = int(time.time()) - int(days) * 86400
        if verdicts:
            tenants = [
                str(r[0] or "")
                for r in (await db.execute(
                    text(
                        "SELECT DISTINCT tenant_id FROM memory_verdicts "
                        "UNION SELECT tenant_id FROM memory_verdict_retention "
                        "UNION SELECT tenant_id FROM memory_verdict_redacted"
                    )
                )).fetchall()
            ]
            for tenant in tenants:
                if await _prune_verdicts(db, tenant, cutoff, execute) < 0:
                    failures += 1
        if tokens:
            if execute:
                result = await db.execute(
                    text(build_token_delete_sql()),
                    {"cutoff": cutoff, "now": int(time.time())},
                )
                await db.commit()
                print(
                    f"pruned memory_action_tokens: {int(result.rowcount or 0)} "
                    f"row(s) older than {days}d"
                )
            else:
                count = int((await db.execute(
                    text(build_token_count_sql()),
                    {"cutoff": cutoff, "now": int(time.time())},
                )).scalar() or 0)
                print(f"dry-run: {count} action token(s) prunable")
        if quarantine:
            if execute:
                result = await db.execute(
                    text(build_quarantine_delete_sql()), {"cutoff": cutoff},
                )
                await db.commit()
                print(
                    f"pruned injection_quarantine: {int(result.rowcount or 0)} "
                    f"row(s) older than {days}d"
                )
            else:
                count = int((await db.execute(
                    text(build_quarantine_count_sql()), {"cutoff": cutoff},
                )).scalar() or 0)
                print(f"dry-run: {count} quarantine row(s) prunable")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--verdicts", action="store_true")
    parser.add_argument("--tokens", action="store_true")
    parser.add_argument("--quarantine", action="store_true")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be >= 1")
    return asyncio.run(_run(
        args.days, args.execute,
        verdicts=args.verdicts, tokens=args.tokens,
        quarantine=args.quarantine,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
