#!/usr/bin/env python
"""backfill_retention_score.py — W5-3（记忆/长程 v2，2026-10-23）回填
memory_concepts.retention_score（PLAN §5.2：默认 dry-run，`--execute` 才写；
WHERE ... IS NULL 幂等，可重复执行）。

用法（cd backend）：
    python -m scripts.backfill_retention_score            # dry-run（默认）
    python -m scripts.backfill_retention_score --execute  # 写库（幂等）

评分口径（纯函数 plan_backfill，0~1）：
    0.5*weight + 0.3*recency + 0.2*recurrence
    recency    = exp(-age_days / max(stability,1))，age 取 last_recurrence_at
                 或 created_at（无时间戳按 age=0）
    recurrence = min(recurrence_count/10, 1.0)
fidelity_state 不回填（默认 'full'，不回溯降级——P2 可逆）。
"""
import argparse
import asyncio
import math
from datetime import datetime


def plan_backfill(rows):
    """纯函数：给定概念行 dict 列表，返回需要回填的 [{id, retention_score}]。
    unset = retention_score 为 NULL 或 0（列 DDL `DEFAULT 0` 使存量行落 0，
    IS NULL 永不命中——0 即未回填态）；已回填（非 0）的行跳过（幂等核心）。"""
    now = datetime.utcnow()
    out = []
    for r in rows:
        existing = r.get("retention_score")
        if existing is not None and float(existing) != 0.0:
            continue
        weight = float(r.get("weight") or 0.5)
        weight = min(max(weight, 0.0), 1.0)
        rec = r.get("last_recurrence_at") or r.get("created_at")
        if rec is not None:
            age_days = max((now - rec).total_seconds() / 86400.0, 0.0)
        else:
            age_days = 0.0
        stability = float(r.get("stability") or 14.0)
        stability = max(stability, 1.0)
        recency = math.exp(-age_days / stability)
        recurrence = min(float(r.get("recurrence_count") or 0) / 10.0, 1.0)
        score = 0.5 * weight + 0.3 * recency + 0.2 * recurrence
        out.append({"id": r["id"], "retention_score": round(score, 6)})
    return out


async def _run(execute: bool) -> int:
    from sqlalchemy import text

    from app.db.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        res = await db.execute(text(
            "SELECT id, weight, stability, recurrence_count, last_recurrence_at,"
            " created_at, retention_score FROM memory_concepts"
            " WHERE retention_score IS NULL OR retention_score = 0"))
        rows = [dict(r) for r in res.mappings().all()]
        plan = plan_backfill(rows)
        print(f"[{'execute' if execute else 'dry-run'}] 待回填 {len(plan)} 行"
              f"（扫描 {len(rows)} 行，已回填（非 0）跳过）")
        for item in plan[:20]:
            print(f"  {item['id'][:8]} -> retention_score={item['retention_score']}")
        if len(plan) > 20:
            print(f"  ... 共 {len(plan)} 行")
        if execute and plan:
            for item in plan:
                await db.execute(
                    text("UPDATE memory_concepts SET retention_score = :s"
                         " WHERE id = :id"
                         " AND (retention_score IS NULL OR retention_score = 0)"),
                    {"s": item["retention_score"], "id": item["id"]})
            await db.commit()
            print(f"[execute] 已写入 {len(plan)} 行（幂等：重跑将命中 0 行）")
        elif not execute:
            print("(dry-run 未写库；加 --execute 执行)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="回填 memory_concepts.retention_score（默认 dry-run）")
    ap.add_argument("--execute", action="store_true", help="应用变更（默认 dry-run）")
    args = ap.parse_args()
    return asyncio.run(_run(args.execute))


if __name__ == "__main__":
    raise SystemExit(main())
