#!/usr/bin/env python
"""memory_baseline_report.py — W0-1/W5-4（记忆/长程 v2，2026-10-23）：7 天
基线报告（tokens/session · cache_hit_ratio · p95 · 注入字符 · 重复工具率 ·
done→reopen 率，按模型分档）+ 告警段（残余/误杀/成本）。

用法（cd backend）：
    python -m scripts.memory_baseline_report [--days 7] [--user UID] [--out PATH]
默认只打印 markdown 到 stdout；--out 写文件。
"""
import argparse
import asyncio


async def _run(days: int, user: str, out: str) -> int:
    from app.db.database import AsyncSessionLocal
    from app.services.memory_baseline_service import (
        collect_baseline_metrics, render_baseline_report)

    async with AsyncSessionLocal() as db:
        data = await collect_baseline_metrics(
            db, days=days, user_id=user or None)
    md = render_baseline_report(data)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(md + "\n")
        print(f"[written] {out}")
    else:
        print(md)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆/长程 v2 基线报告（7 天窗口）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--user", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    return asyncio.run(_run(args.days, args.user, args.out))


if __name__ == "__main__":
    raise SystemExit(main())
