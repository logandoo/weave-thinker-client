#!/usr/bin/env python3
"""记忆召回探针（批次 A 运行时验证，2026-09-14）。

对指定用户跑一组真实查询（短/长/时间三类）走完整 v2 检索管线
（stage0→1→2→3→4→注入），输出每条：耗时 / 注入字符 / 候选数 / top gate 分 /
内容预览；长查询与时间查询附「stage0 LLM 开/关」A/B（关=monkeypatch 归一化
直接失败 → 既有 jieba 回退），量化 stage0 成本与效果差。

只读业务数据；召回 boost 与 read 遥测为正常副作用（fire-and-forget）。

用法（backend/ 目录）:
    ../.venv/bin/python -m scripts.memory_recall_probe [--user test]
"""

import argparse
import asyncio
import sys
import time

from sqlalchemy import text

from app.db.database import AsyncSessionLocal
import app.services.memory_retrieval_service as mrs

QUERIES = [
    ("短", "我喜欢什么编辑器"),
    ("短", "我养了什么宠物"),
    ("短", "回答我时应该长一点还是短一点"),
    ("长", "帮我总结一下我最近在记忆系统方面做过哪些改进和验证工作"),
    ("时间", "我最近都聊了些什么？"),
    ("长", "PostgreSQL 部署和连接池要注意什么"),
]


async def _run_once(db, uid: str, q: str) -> tuple[float, str, list, float]:
    t0 = time.perf_counter()
    ctx, ids, top = await mrs.retrieve_with_meta(db, uid, [{"role": "user", "content": q}])
    dt = (time.perf_counter() - t0) * 1000
    return dt, ctx, ids, top


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="test")
    args = ap.parse_args()

    async with AsyncSessionLocal() as db:
        uid = (await db.execute(
            text("SELECT id FROM users WHERE username = :u"), {"u": args.user}
        )).scalar()
        if not uid:
            print(f"user not found: {args.user}")
            return 1

        stats = (await db.execute(text(
            "SELECT "
            "(SELECT count(*) FROM memory_concepts WHERE user_id=:uid AND status='active' AND valid_to IS NULL), "
            "(SELECT count(*) FROM memory_episodes WHERE user_id=:uid AND valid_to IS NULL), "
            "(SELECT count(*) FROM subconscious_log WHERE user_id=:uid)"
        ), {"uid": uid})).fetchone()
        print(f"user={args.user} id={uid[:8]} | active concepts={stats[0]} episodes={stats[1]} subconscious={stats[2]}")
        print("=" * 72)

        # 预热：BM25 索引构建/embedding 连接不计入测量
        await _run_once(db, uid, "预热查询")

        results = []
        for kind, q in QUERIES:
            mrs._session_cache.clear()
            dt, ctx, ids, top = await _run_once(db, uid, q)
            results.append((kind, q, dt, ctx, ids, top))
            print(f"\n[{kind}] {q}")
            print(f"  耗时 {dt:.0f}ms | 注入 {len(ctx)} 字符 | 候选 {len(ids)} | top={top:.3f}")
            preview = (ctx[:260].replace("\n", " ") or "(空)")
            print(f"  预览: {preview}{'…' if len(ctx) > 260 else ''}")

            # stage0 A/B：长/时间查询对比 LLM 归一化开/关
            if kind in ("长", "时间"):
                mrs._session_cache.clear()
                orig = mrs._llm_time_normalization

                async def _fail(*a, **kw):
                    raise RuntimeError("probe: stage0 llm disabled")

                mrs._llm_time_normalization = _fail
                try:
                    dt2, ctx2, ids2, top2 = await _run_once(db, uid, q)
                finally:
                    mrs._llm_time_normalization = orig
                print(f"  A/B: stage0关 {dt2:.0f}ms（省 {dt - dt2:+.0f}ms）| 注入 {len(ctx2)} 字符 | 候选 {len(ids2)} | top={top2:.3f} | ctx差异={'有' if ctx2 != ctx else '无'}")

        # 汇总
        print("\n" + "=" * 72)
        print("汇总:")
        for kind, q, dt, ctx, ids, top in results:
            print(f"  {kind:2s} {dt:7.0f}ms  chars={len(ctx):4d}  ids={len(ids):2d}  top={top:.3f}  {q[:28]}")
        over1500 = sum(1 for *_, ctx, _, _ in results if len(ctx) > 1500)
        print(f"  注入 >1500 字符（A3 旧截断会触发的比例）: {over1500}/{len(results)}")
        # 留时间给 fire-and-forget（boost/read 遥测）在本事件循环内落库
        await asyncio.sleep(2)
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
