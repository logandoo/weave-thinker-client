"""本地孤儿消息清理（下游 L1，2026-11-05）。

针对 P0 修复前 `_delete_message_slice` 零 delete 事件留下的历史孤儿：
assistant 消息、正文为空、无工具结果/调用、且同一会话中它之前没有任何 user 消息。
典型事故：conv d7ea65b8 的 f697cf2d（content="" / reasoning=419 / final），
排在编辑后的新 query 之前，客户端显示为首条“没有 query 的空回答”。

只清**本地** DB（默认 dry-run；--execute 才删除）。它不补同步事件/墓碑——
跨设备存量修复由上游 P3 `scripts/backfill_message_tombstones.py` 在服务端完成；
本脚本是离线/单机兜底，避免把“服务端已经不存在、客户端还留着”的孤儿继续展示。

用法（在 server/ 目录，指向本地 sidecar 配置）：
    LOCALSTACK_CONFIG_PATH="$HOME/Library/Application Support/weave-thinker-client/localstack/app/config.toml" \
      ../.localstack/venv/bin/python -m scripts.cleanup_orphan_messages
    ... --execute
    ... --conversation d7ea65b8-9241-4112-bd5d-afe87338b400 --execute
    ... --id f697cf2d-91ee-4072-b821-a94a9aad1986 --execute

注意：不要对云端生产库直接跑本脚本（云端修复走 backfill_message_tombstones）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.orm import aliased

from app.db.database import (
    AsyncSessionLocal, Conversation, Message, SubconsciousLog, SyncEvent,
)


def _empty(expr):
    return func.coalesce(func.trim(expr), "") == ""


async def find_orphans(db, conversation_id: str | None = None,
                       message_id: str | None = None):
    U = aliased(Message)
    prior_user = exists().where(
        and_(
            U.conversation_id == Message.conversation_id,
            U.role == "user",
            or_(
                U.created_at < Message.created_at,
                and_(U.created_at == Message.created_at, U.id < Message.id),
            ),
        )
    )
    stmt = select(Message).where(
        Message.role == "assistant",
        _empty(Message.content),
        or_(_empty(Message.tool_results), func.trim(Message.tool_results) == "[]"),
        or_(_empty(Message.tool_calls), func.trim(Message.tool_calls) == "[]"),
        Message.delivery_status != "streaming",
        ~prior_user,
    ).order_by(Message.created_at, Message.id)
    if conversation_id:
        stmt = stmt.where(Message.conversation_id == conversation_id)
    if message_id:
        stmt = stmt.where(Message.id == message_id)
    return (await db.execute(stmt)).scalars().all()


async def run(conversation_id: str | None, message_id: str | None,
              execute: bool) -> int:
    async with AsyncSessionLocal() as db:
        rows = await find_orphans(db, conversation_id, message_id)
        print(f"[cleanup_orphan_messages] candidates={len(rows)} "
              f"(assistant · 空正文 · 无工具 · 无前置 user)")
        for row in rows[:20]:
            print(f"  - conv={row.conversation_id} msg={row.id} "
                  f"status={row.delivery_status} created={row.created_at} "
                  f"reasoning={len(row.reasoning_content or '')}B")
        if len(rows) > 20:
            print(f"  … 其余 {len(rows) - 20} 条省略")
        if not rows:
            return 0
        if not execute:
            print("DRY-RUN: nothing deleted (rerun with --execute to apply)")
            return 0
        ids = [row.id for row in rows]
        users = set()
        for row in rows:
            try:
                uid = (await db.execute(
                    select(Conversation.user_id).where(
                        Conversation.id == row.conversation_id)
                )).scalar_one_or_none()
                if not uid:
                    continue
                users.add(uid)
                # 清本地待推 create（非 rx），防下一次 push 把孤儿复活到云端。
                await db.execute(
                    delete(SyncEvent).where(
                        SyncEvent.user_id == uid,
                        SyncEvent.entity_type == "messages",
                        SyncEvent.entity_id == row.id,
                        ~func.coalesce(SyncEvent.origin_device, "").like("rx:%"),
                    )
                )
                # 清本地派生记忆（source_ids JSON 引用该 message id）。
                await db.execute(
                    delete(SubconsciousLog).where(
                        SubconsciousLog.user_id == uid,
                        SubconsciousLog.source_ids.like(f'%"{row.id}"%'),
                    )
                )
                # 失效该会话的 rolling summary / resume 快照。
                from app.services.context_continuity_service import invalidate_continuity
                await invalidate_continuity(db, str(row.conversation_id))
            except Exception as exc:  # noqa: BLE001
                print(f"  ! purge skip {row.id}: {exc}")
        for i in range(0, len(ids), 500):
            await db.execute(delete(Message).where(Message.id.in_(ids[i:i + 500])))
        await db.commit()
        try:
            from app.services.memory_retrieval_service import invalidate_session_cache_for_user
            for uid in users:
                invalidate_session_cache_for_user(uid)
        except Exception:  # noqa: BLE001
            pass
        print(f"[cleanup_orphan_messages] deleted={len(ids)} (local only; "
              f"cross-device backfill = upstream scripts/backfill_message_tombstones.py)")
        return len(ids)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--conversation", default=None)
    ap.add_argument("--id", default=None)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args(argv)
    try:
        asyncio.run(run(args.conversation, args.id, args.execute))
    except Exception as exc:  # noqa: BLE001
        print(f"[cleanup_orphan_messages] failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
