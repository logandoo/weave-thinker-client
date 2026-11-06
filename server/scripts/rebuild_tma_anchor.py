"""Verify a tenant verdict chain, then rebuild its anchor/witness from the DB.

This is the operator escape hatch for a lost external medium or a DB restore.
It NEVER writes when the chain is broken.  Dry-run is the default.

Usage (from backend/):
    python -m scripts.rebuild_tma_anchor --tenant <segment>
    python -m scripts.rebuild_tma_anchor --tenant <segment> --execute
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import text

from app.db.database import AsyncSessionLocal
from app.services import verdict_log_service as v
from app.services.tenant_service import tenant_segment


async def _main(
    tenant: str, execute: bool, accept_truncation: bool = False,
) -> int:
    async with AsyncSessionLocal() as db:
        ok, broken = await v.verify_chain(
            db, tenant, require_nonempty=True, allow_missing_witness=True,
        )
        if not ok:
            print(
                f"refusing rebuild: verdict chain BROKEN tenant={tenant!r} "
                f"first_bad={broken!r}",
                file=sys.stderr,
            )
            return 1
        rows = (await db.execute(
            text(
                "SELECT tenant_id, seq, merkle_root, last_hash, sig, key_id, "
                "witness_required, created_at_epoch FROM memory_verdict_checkpoints "
                "WHERE tenant_id = :tenant ORDER BY seq ASC"
            ),
            {"tenant": tenant},
        )).fetchall()
        chain = []
        for row in rows:
            entry = {
                "tenant_id": row[0],
                "seq": int(row[1] or 0),
                "merkle_root": row[2],
                "last_hash": row[3],
                "sig": row[4],
                "key_id": row[5],
                "created_at_epoch": int(row[7] or 0),
            }
            entry["witness_required"] = bool(row[6])
            chain.append(entry)
        head = (await db.execute(
            text(
                "SELECT last_hash, last_seq FROM memory_verdict_heads "
                "WHERE tenant_id = :tenant"
            ),
            {"tenant": tenant},
        )).fetchone()
        head_hash = str(head[0] or "") if head else ""
        head_seq = int(head[1] or 0) if head else 0

        anchor = v.read_anchor(tenant) or v.read_anchor_copy(tenant)
        head_witness = v.read_head_witness(tenant) or v.read_head_witness_copy(tenant)
        if not accept_truncation:
            if head_witness and int(head_witness.get("seq") or 0) > head_seq:
                print(
                    f"refusing rebuild: head witness is ahead of DB "
                    f"tenant={tenant!r} witness_seq={head_witness.get('seq')} "
                    f"head_seq={head_seq}; pass --accept-truncation only for an "
                    "explicit restore",
                    file=sys.stderr,
                )
                return 1
        if anchor and not accept_truncation:
            anchor_entries = [
                c for c in (anchor.get("chain") or [])
                if isinstance(c, dict)
            ] or [anchor]
            anchor_by_seq = {
                int(c.get("seq") or 0): c for c in anchor_entries
            }
            db_by_seq = {c["seq"]: c for c in chain}
            missing = {s for s in anchor_by_seq if s > 0} - set(db_by_seq)
            if int(anchor.get("seq") or 0) > head_seq or missing:
                print(
                    f"refusing rebuild: anchor is ahead/missing in DB "
                    f"tenant={tenant!r} anchor_seq={anchor.get('seq')} "
                    f"head_seq={head_seq} missing={sorted(missing)}; "
                    "pass --accept-truncation only for an explicit restore",
                    file=sys.stderr,
                )
                return 1
            for seq, anchored in anchor_by_seq.items():
                current = db_by_seq.get(seq)
                if current is None:
                    continue
                for field in (
                    "tenant_id", "merkle_root", "last_hash", "key_id", "sig",
                ):
                    if anchored.get(field) != current.get(field):
                        print(
                            f"refusing rebuild: anchor checkpoint {seq} "
                            f"content mismatch ({field}) tenant={tenant!r}",
                            file=sys.stderr,
                        )
                        return 1
        if not execute:
            print(
                f"dry-run: chain OK tenant={tenant!r} checkpoints={len(chain)} "
                f"head_seq={head_seq}; pass --execute to rewrite anchor+witness"
            )
            return 0
        if chain:
            v.write_anchor_chain(tenant, chain)
        if head_seq:
            v.write_head_witness(tenant, head_seq, head_hash)
        print(
            f"rebuilt tenant={tenant!r} checkpoints={len(chain)} "
            f"head_seq={head_seq} "
            f"witness={'configured' if v.witness_copy_dir() else 'not-configured'}"
        )
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--accept-truncation", action="store_true",
        help="explicit restore: allow the anchor to be ahead of the DB",
    )
    args = parser.parse_args(argv)
    tenant = args.tenant or tenant_segment()
    return asyncio.run(_main(
        tenant, args.execute, accept_truncation=args.accept_truncation,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
