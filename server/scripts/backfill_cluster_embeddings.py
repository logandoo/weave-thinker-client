#!/usr/bin/env python3
"""回填 memory_clusters.embedding（批次 A / N1，2026-09-14）。

背景：`memory_cluster_service._update_cluster_embedding` 旧实现读
`config.memory["embedding_dim"]`（键不存在 → 默认 1536），与 1024 维端点
永不匹配 → 集群 embedding 全 NULL（本地 20/20 实证），
`_find_nearest_cluster` 以 NULL 距离任意选簇。修复维度来源后，本脚本对
NULL 集群按成员均值回填，并写入 embedding_model 溯源（DC2）。

幂等：只处理 embedding IS NULL 的集群；可重复执行。

用法（backend/ 目录）:
    ../.venv/bin/python -m scripts.backfill_cluster_embeddings            # dry-run
    ../.venv/bin/python -m scripts.backfill_cluster_embeddings --execute
"""

import asyncio
import sys

from sqlalchemy import text

from app.db.database import AsyncSessionLocal
from app.services.memory_cluster_service import _update_cluster_embedding

_EXECUTE = "--execute" in sys.argv


async def main() -> None:
    print(f"模式: {'EXECUTE' if _EXECUTE else 'DRY-RUN'}")
    print("=" * 60)

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT id, user_id, name, member_count FROM memory_clusters "
            "WHERE embedding IS NULL ORDER BY member_count DESC"
        ))).fetchall()
        print(f"待回填集群: {len(rows)}")

        ok, skipped, failed = 0, 0, 0
        for cid, uid, name, mc in rows:
            if not _EXECUTE:
                print(f"  [dry-run] {cid[:8]} {name!r} member_count={mc}")
                continue
            try:
                written = await _update_cluster_embedding(db, cid)
                await db.commit()
                if written:
                    ok += 1
                else:
                    skipped += 1
                    print(f"  [skip] {cid[:8]} {name!r}: 无可用成员向量（保持 NULL）")
            except Exception as exc:  # noqa: BLE001 - 单簇失败不阻塞其余
                await db.rollback()
                failed += 1
                print(f"  [fail] {cid[:8]} {name!r}: {exc}")

        if _EXECUTE:
            left = (await db.execute(
                text("SELECT COUNT(*) FROM memory_clusters WHERE embedding IS NULL")
            )).scalar()
            print(f"完成: 回填 {ok}/{len(rows)}（跳过 {skipped}，失败 {failed}）；剩余 NULL = {left}")
        else:
            print("（dry-run 未写库；加 --execute 执行）")


if __name__ == "__main__":
    asyncio.run(main())
