"""Backfill: externalize oversized tool_results / reasoning_content / tool_calls
already stored inline in `messages` rows (P1-2 of DESIGN_conv_open_performance).

Default dry-run. Usage (from backend/):
    python -m scripts.backfill_message_payloads              # dry-run
    python -m scripts.backfill_message_payloads --execute    # apply
    python -m scripts.backfill_message_payloads --execute --batch 100

Idempotent: rows whose field is already an externalized stub are skipped.
Per-message rows（W3 项2，2026-10-06）：跨消息 sha 去重已废除——旧 skipped_dup
契约让 stub.payload_ref 指向他消息的 payload 行，路由按 (message_id, field)
归属寻址后这些消息永久 404 孤儿。重复内容也各写本消息自己的行（唯一键
uq_message_payloads_msg_field 已支持）。Failed rows are counted and skipped
(safe to re-run).
"""
import argparse
import asyncio
import json
import sys

from sqlalchemy import or_, select, text

# [C] process_row 纯逻辑（可单测）
def process_row(field: str, value: str, existing_sha: set, max_chars: int, make_stub, compute_sha):
    """Return (new_value, payload_tuple|None, status).

    payload_tuple = (field, content, size_bytes, sha256)
    status ∈ externalized | skipped_stub | skipped_thin | duplicated

    duplicated（W3 项2）：内容与既有他消息 payload 同 sha——**仍返回本消息
    payload_tuple**（调用方写本消息行），去重语义只作统计标记。
    """
    from app.services.message_payload_service import is_externalized_stub

    if not value or not isinstance(value, str):
        return value, None, "skipped_thin"
    if is_externalized_stub(value):
        return value, None, "skipped_stub"
    if len(value) <= max_chars:
        return value, None, "skipped_thin"
    sha = compute_sha(value)
    stub = make_stub(field, value)
    if sha in existing_sha:
        # 重复内容：不得复用他消息行（归属寻址会 404）——照样产出本消息行
        return stub, (field, value, len(value), sha), "duplicated"
    existing_sha.add(sha)
    return stub, (field, value, len(value), sha), "externalized"


async def _repair_orphans(db, execute: bool) -> tuple:
    """孤儿桩修复（W3 项2 修复器，2026-10-06）：旧 skipped_dup 契约的存量产物。

    对每个外置桩字段：本消息 (message_id, field) 无 payload 行、但同 payload_ref
    (sha256) 的兄弟行存在 → 复制兄弟行内容为本消息行（内容寻址的合法回收）。
    无兄弟行（内容真丢）=不可修复，计数报告。返回 (repaired, unrecoverable)。
    dry-run 只统计不写。
    """
    from app.db.database import Message, MessagePayload
    from app.services.message_payload_service import is_externalized_stub

    repaired = unrecoverable = 0
    last_id = ""
    while True:
        rows = (await db.execute(
            select(Message.id, Message.tool_results, Message.reasoning_content, Message.tool_calls)
            .where(or_(
                text("tool_results LIKE '%_externalized%'"),
                text("reasoning_content LIKE '%_externalized%'"),
                text("tool_calls LIKE '%_externalized%'"),
            ))
            .where(Message.id > last_id)
            .order_by(Message.id)
            .limit(200)
        )).all()
        if not rows:
            break
        for mid, tr, rc, tc in rows:
            last_id = mid
            for field, val in (("tool_results", tr), ("reasoning_content", rc), ("tool_calls", tc)):
                if not val or not is_externalized_stub(val):
                    continue
                own = (await db.execute(select(MessagePayload.id).where(
                    MessagePayload.message_id == mid, MessagePayload.field == field))).first()
                if own:
                    continue
                ref = None
                try:
                    ref = json.loads(val).get("payload_ref")
                except (ValueError, TypeError):
                    ref = None
                if not ref:
                    continue
                sibling = (await db.execute(select(
                    MessagePayload.content, MessagePayload.size_bytes
                ).where(MessagePayload.sha256 == ref).limit(1))).first()
                if not sibling:
                    unrecoverable += 1
                    continue
                if execute:
                    try:
                        async with db.begin_nested():
                            db.add(MessagePayload(
                                message_id=mid, field=field, content=sibling[0],
                                size_bytes=sibling[1], sha256=ref,
                            ))
                    except Exception as exc:  # noqa: BLE001 — 行级失败不阻塞批（与主循环对称）
                        unrecoverable += 1
                        print(f"[backfill] orphan-repair FAILED {mid}/{field}: {exc}")
                        continue
                repaired += 1
        if execute:
            await db.commit()
    return repaired, unrecoverable


async def run(execute: bool, batch: int) -> int:
    from app.core.config import get_config
    from app.db.database import AsyncSessionLocal, Message, MessagePayload
    from app.services.message_payload_service import (
        build_field_stub,
        compute_sha256,
        is_externalized_stub,
        should_externalize,
    )
    from app.services.message_payload_service import _inline_max_chars

    max_chars = _inline_max_chars()
    print(f"[backfill] mode={'EXECUTE' if execute else 'DRY-RUN'} batch={batch} threshold={max_chars}")

    scanned = externalized = duplicated = skipped = failed = 0
    async with AsyncSessionLocal() as db:
        # Phase 0（W3 项2）：孤儿桩修复——旧 skipped_dup 契约的存量消息
        # (message_id, field) 无行但兄弟 sha 行在 → 复制回收（修复归属 404）。
        repaired, unrecoverable = await _repair_orphans(db, execute)
        if repaired or unrecoverable:
            print(f"[backfill] orphan-repair: repaired={repaired} "
                  f"unrecoverable(no sibling)={unrecoverable}")

        existing = (await db.execute(select(MessagePayload.sha256))).scalars().all()
        existing_sha = set(existing)
        print(f"[backfill] existing payloads: {len(existing_sha)}")

        last_id = ""
        while True:
            # keyset 分页（不用 OFFSET）：execute 会原地收缩匹配集，OFFSET 会跳行。
            rows = (await db.execute(
                select(Message.id, Message.tool_results, Message.reasoning_content, Message.tool_calls)
                .where(or_(
                    text("length(coalesce(tool_results,'')) > :t"),
                    text("length(coalesce(reasoning_content,'')) > :t"),
                    text("length(coalesce(tool_calls,'')) > :t"),
                ))
                .where(Message.id > last_id)
                .params(t=max_chars)
                .order_by(Message.id)
                .limit(batch)
            )).all()
            if not rows:
                break
            for mid, tr, rc, tc in rows:
                last_id = mid
                scanned += 1
                # A4.9 Minor（评审 A3）：行级 savepoint——flush 冲突只回滚本行，
                # 不毒化 session（否则后续行全 PendingRollbackError、批 commit 炸）。
                row_stats = {"externalized": 0, "duplicated": 0, "skipped": 0}
                try:
                    async with db.begin_nested():
                        updates = {}
                        for field, val in (("tool_results", tr), ("reasoning_content", rc), ("tool_calls", tc)):
                            new_val, payload, status = process_row(
                                field, val, existing_sha, max_chars,
                                build_field_stub, compute_sha256,
                            )
                            if status in ("externalized", "duplicated"):
                                # W3 项2：duplicated 与 externalized 同路径——都写
                                # **本消息**自己的 (message_id, field) 行+桩
                                if execute:
                                    db.add(MessagePayload(
                                        message_id=mid, field=payload[0], content=payload[1],
                                        size_bytes=payload[2], sha256=payload[3],
                                    ))
                                    updates[field] = new_val
                                row_stats[status] += 1
                            else:
                                row_stats["skipped"] += 1
                        if execute and updates:
                            await db.execute(
                                Message.__table__.update().where(Message.id == mid).values(**updates)
                            )
                    # savepoint 成功才计分（失败的写已回滚）
                    externalized += row_stats["externalized"]
                    duplicated += row_stats["duplicated"]
                    skipped += row_stats["skipped"]
                except Exception as exc:  # noqa: BLE001 — 行级失败不阻塞批
                    failed += 1
                    print(f"[backfill] FAILED {mid}: {exc}")
            if execute:
                await db.commit()

    print(f"[backfill] scanned={scanned} externalized={externalized} duplicated={duplicated} "
          f"skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="apply changes (default dry-run)")
    ap.add_argument("--batch", type=int, default=50)
    args = ap.parse_args()
    return asyncio.run(run(args.execute, args.batch))


if __name__ == "__main__":
    sys.exit(main())
