"""sync 历史保留工具（UPSTREAM_TODO_20261007 项8，W7 评审 A/B；W8 8a/8d 收口）。

按龄删除 `sync_events`（事件日志）。默认 dry-run 只报数；`--execute` 真删。
删除后保留沿（`min_available_seq`）抬升 / per-user 裁剪水位（`sync_prune_watermarks`）
推进——游标越窗的客户端从 `/api/sync/delta` 收到 `resync_required=true`，应放弃
增量、改走全量重同步（`/api/sync/resync`，其 messages 快照已解析全文，见项6.1）。

⚠ 操作须知：
- 保留窗应显著长于最慢设备的同步周期（对齐 Joplin 180d 范式；默认 90 天）。
- **墓碑不随龄裁剪**（复审裁定 D-w4）：resync 为 upsert 合并、不做本地删除对账，
  删墓碑只会加重越窗设备的删除分歧（被删实体可能复活）；墓碑行极小，保留成本
  可忽略。resync 现已回放墓碑（W8 8b），删除语义经全量路径可恢复。
- **push 侧保护**（W8 8d，D-w8-4/D-w8-6）：仍可能被 `_push` 发送的事件不裁剪——
  绑定在场（`sync_state.server_url`+`cloud_username`；解绑可续推故不因停用/清密码
  解除）且 `seq > push_cursor` 且 `origin` 非 `rx:`（与客户端 `_push` 选择面同源）。
  dry-run 打印 candidates/protected/deletable 预览，执行前可核对保护面。
- 旧客户端（未读 `resync_required`）越窗后会静默漏事件——建议仅在确认设备
  近期在线（或愿意手动全量重同步）时执行。

用法（from backend/）：
    python -m scripts.prune_sync_events                 # dry-run（默认 90 天）
    python -m scripts.prune_sync_events --days 30
    python -m scripts.prune_sync_events --days 90 --execute
"""
import argparse
import asyncio
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.db.database import AsyncSessionLocal, SyncTombstone


async def _main(days: int, execute: bool) -> None:
    if days <= 0:
        print("[prune_sync_events] days<=0 → no-op（保留关闭语义）")
        return

    from app.services.sync_retention import (
        prune_sync_history,
        prune_sync_preview,
        sync_history_stats,
    )

    cutoff = datetime.utcnow() - timedelta(days=days)
    async with AsyncSessionLocal() as db:
        stats_before = await sync_history_stats(db)
        prev = await prune_sync_preview(db, days)
        tb_total = (await db.execute(
            select(func.count()).select_from(SyncTombstone)
        )).scalar() or 0
        print(f"[prune_sync_events] days={days} cutoff={cutoff.isoformat()}Z")
        print(f"  candidates: events={prev['candidates']} "
              f"(protected unpushed={prev['protected_unpushed']} · "
              f"deletable={prev['deletable']}) | tombstones retained={int(tb_total)}"
              " (不裁剪，D-w4)")
        print(f"  retained seq window before: min={stats_before['min_available_seq']} "
              f"max={stats_before['max_seq']}")
        if not execute:
            print("DRY-RUN: nothing deleted (rerun with --execute to apply)")
            return
        out = await prune_sync_history(db, days)
        stats_after = await sync_history_stats(db)
        print(f"  deleted: events={out['deleted_events']} | "
              f"protected unpushed(bound users)={out.get('protected_unpushed', 0)}")
        print(f"  watermark users advanced/created={out.get('watermark_users', 0)}")
        print(f"  retained seq window after: min={stats_after['min_available_seq']} "
              f"max={stats_after['max_seq']}")
        print("  note: clients with cursor below min_available_seq / prune watermark "
              "will receive resync_required=true from /api/sync/delta — full resync "
              "expected (its response now replays tombstones, W8 8b).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="按龄裁剪 sync 事件日志（墓碑不裁剪）")
    ap.add_argument("--days", type=int, default=90, help="保留天数（默认 90；<=0 = no-op）")
    ap.add_argument("--execute", action="store_true", help="真删（默认 dry-run）")
    args = ap.parse_args()
    asyncio.run(_main(args.days, args.execute))
