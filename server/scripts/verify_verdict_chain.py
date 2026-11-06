"""Verify the TMA-NM P1a verdict hash chain for one tenant.

Usage (from backend/):
    python -m scripts.verify_verdict_chain [tenant_id]

Exit code 0 = chain intact; 1 = tampered/broken (prints the first bad record).
"""
from __future__ import annotations

import asyncio
import sys

from app.db.database import AsyncSessionLocal
from app.services.tenant_service import tenant_segment
from app.services.verdict_log_service import (
    chain_key_status,
    read_anchor,
    verify_chain,
)


async def _main(tenant_id: str) -> int:
    async with AsyncSessionLocal() as db:
        ok, broken = await verify_chain(db, tenant_id, require_nonempty=True)
        key_status = await chain_key_status(db, tenant_id)
    if not ok and broken == "missing":
        print(
            f"verdict chain MISSING tenant={tenant_id!r} key_status={key_status} "
            "(no head row/records; pass the stored tenant segment, not a raw id)"
        )
        return 1
    if ok:
        anchor = read_anchor(tenant_id)
        checkpoint = anchor.get("seq") if anchor else "none"
        print(
            f"verdict chain OK tenant={tenant_id!r} key_status={key_status} "
            f"checkpoint={checkpoint}"
        )
        if key_status == "dev":
            print(
                "WARNING: key_status=dev — the chain is structurally intact but "
                "forgeable with the repo-public dev key; do not treat it as authority"
            )
        elif key_status == "unknown":
            print(
                "WARNING: key_status=unknown — at least one historical key id is "
                "not resolvable in tma_verdict_keyring; the chain is not fully "
                "verifiable until that key is restored"
            )
        return 0
    print(
        f"verdict chain BROKEN tenant={tenant_id!r} first_bad={broken!r} "
        f"key_status={key_status}"
    )
    return 1


if __name__ == "__main__":
    # Pass the STORED tenant segment (as written by tenant_segment()); do not
    # normalise it again — a 48+ char segment would be re-hashed and the CLI
    # would verify an unrelated empty chain.
    tenant = sys.argv[1] if len(sys.argv) > 1 else tenant_segment()
    raise SystemExit(asyncio.run(_main(tenant)))
