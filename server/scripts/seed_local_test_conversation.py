#!/usr/bin/env python3
"""Seed one local conversation for the `test` account.

The non-hermetic suite (excluded from CI) includes tests that need a real
conversation for the documented test account (``test`` / ``123456``):

  * tests/test_memory_batch_cdef_fixes.py::test_session_search_read_mode_real_db

It is idempotent: the conversation is created once and topped up to the
required message count.  Raw SQL is used on purpose — ORM writes would emit
sync-capture events for a developer database.

Usage (from backend/, with the project venv):
  python -m scripts.seed_local_test_conversation
  python -m scripts.seed_local_test_conversation --messages 12
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

sys.path.insert(0, ".")

from sqlalchemy import text

TITLE = "本地测试会话（seed）"


async def _seed(messages: int) -> int:
    from app.db.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        uid = (await db.execute(text(
            "SELECT id FROM users WHERE username='test'"))).scalar()
        if not uid:
            print("test user not found — register it first (test/123456)",
                  file=sys.stderr)
            return 2
        cid = (await db.execute(text(
            "SELECT id FROM conversations WHERE user_id=:u AND title=:t "
            "ORDER BY created_at LIMIT 1"), {"u": uid, "t": TITLE})).scalar()
        if not cid:
            cid = str(uuid.uuid4())
            await db.execute(text(
                "INSERT INTO conversations (id, user_id, title, created_at, updated_at) "
                "VALUES (:id, :u, :t, NOW(), NOW())"), {"id": cid, "u": uid, "t": TITLE})
        have = (await db.execute(text(
            "SELECT count(*) FROM messages WHERE conversation_id=:c"),
            {"c": cid})).scalar() or 0
        for i in range(have, messages):
            await db.execute(text(
                "INSERT INTO messages (id, conversation_id, role, content, created_at) "
                "VALUES (:id, :c, :r, :content, NOW() - (:age || ' seconds')::interval)"),
                {"id": str(uuid.uuid4()), "c": cid,
                 "r": "user" if i % 2 == 0 else "assistant",
                 "content": f"seed message {i + 1}",
                 "age": str((messages - i) * 10)})
        await db.commit()
        print(f"seeded conversation {cid} for test user {uid}: {max(have, messages)} messages")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages", type=int, default=8)
    args = ap.parse_args()
    return asyncio.run(_seed(args.messages))


if __name__ == "__main__":
    raise SystemExit(main())
