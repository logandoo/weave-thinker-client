"""D3 影子对照（只读）— cosine-gated 合并快路径的 LLM 调用量下降度量。

对每个有概念的用户运行与 consolidation 相同的 ANN 候选对查询，按距离分桶：
  - dist < merge_gray_zone_low（默认 0.03）→ 快路径（无 LLM 直接合并）
  - [low, consolidation_dedup_threshold) → 灰区（仍需 LLM 判定）
报告快路径占比与 LLM 批次下降（旧实现全部对走 LLM）。

运行: cd backend && ../.venv/bin/python -m scripts.d3_merge_shadow
只读：不写任何表。
"""
import asyncio
import sys

from sqlalchemy import text

from app.core.config import get_config
from app.db.database import AsyncSessionLocal

config = get_config()


async def main() -> int:
    low = float(config.memory_retrieval.get("merge_gray_zone_low", 0.03))
    thresh = float(config.memory.get("consolidation_dedup_threshold", 0.08))
    async with AsyncSessionLocal() as db:
        users = (await db.execute(text(
            "SELECT user_id, COUNT(*) FROM memory_concepts "
            "WHERE status IN ('active','silent') AND valid_to IS NULL "
            "AND embedding IS NOT NULL GROUP BY user_id HAVING COUNT(*) > 1"
        ))).fetchall()
        per_user_pairs: dict[str, list[float]] = {}
        for uid, n_concepts in users:
            pairs = (await db.execute(text("""
                SELECT a.id, b.id, (a.embedding <=> b.embedding) AS dist,
                       a.canonical_name, b.canonical_name
                FROM memory_concepts a
                CROSS JOIN LATERAL (
                    SELECT id, embedding, canonical_name FROM memory_concepts
                    WHERE user_id = a.user_id AND status IN ('active','silent')
                      AND valid_to IS NULL AND id != a.id AND embedding IS NOT NULL
                    ORDER BY embedding <=> a.embedding LIMIT 5
                ) b
                WHERE a.user_id = :uid AND a.status IN ('active','silent')
                  AND a.valid_to IS NULL AND a.embedding IS NOT NULL
                  AND (a.embedding <=> b.embedding) < :thresh AND a.id < b.id
            """), {"uid": uid, "thresh": thresh})).fetchall()
            if pairs:
                per_user_pairs[uid] = [(float(r[2]), r[3], r[4]) for r in pairs]

        from app.services.memory_consolidation_service import _fast_merge_name_safe

        def batches(n):
            return (n + 9) // 10
        total_pairs = sum(len(v) for v in per_user_pairs.values())
        old_batches = sum(batches(len(v)) for v in per_user_pairs.values())
        print(f"thresh={thresh} users_with_pairs={len(per_user_pairs)} pairs={total_pairs} "
              f"old_llm_batches={old_batches}")
        print("low | fast(safe) | unsafe_guard | gray | new_batches | reduction")
        for low_i in (0.02, 0.03, 0.04, 0.05, 0.06):
            fast = guard = 0
            new_batches = 0
            for v in per_user_pairs.values():
                gray_n = 0
                for d, na, nb in v:
                    if d < low_i:
                        if _fast_merge_name_safe(na, nb):
                            fast += 1
                        else:
                            guard += 1
                            gray_n += 1
                    else:
                        gray_n += 1
                new_batches += batches(gray_n)
            red = (1 - new_batches / old_batches) * 100 if old_batches else 0.0
            print(f"{low_i:.2f} | {fast} | {guard} | {total_pairs - fast} | {new_batches} | {red:.1f}%")
        print("per-user (uid8, pairs, dists, safe):")
        for uid, pairs in sorted(per_user_pairs.items(), key=lambda kv: -len(kv[1]))[:20]:
            print("  ", uid[:8], len(pairs), [(round(d, 4), _fast_merge_name_safe(a, b))
                                              for d, a, b in sorted(pairs)])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
