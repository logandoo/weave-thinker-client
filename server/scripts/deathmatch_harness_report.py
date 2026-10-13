#!/usr/bin/env python3
"""
B4 (report-only v1, 2026-08-30): deathmatch harness 复盘报告.

读取 deathmatch_harness_runs（终态归档）+ conversations 死磕字段，聚合：
- 终态分布 / 平均轮数 / 停滞直方图
- verifier 复发问题类（≥3 次 = harness 变更候选）
- 单组件有界变更提案模板 + train/dev 验收门清单（AutoDesign 双集隔离 +
  JIT-Agent 三轴准则：reward↑ 且 (latency↓ 或 cost↓) 且其他维度不降）

**Report-only**：只出报告与提案，绝不自动修改 harness——归档数据未积累到
统计意义前，自动外环没有依据（用户裁决 2026-08-30）。

用法:
    cd backend && python -m scripts.deathmatch_harness_report [--days 30] [--out ../tests/deathmatch_evals/harness_report.md]
    CLEANUP_DB_URL="postgresql+asyncpg://..." python -m scripts.deathmatch_harness_report
"""
import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, Iterable, List

sys.path.insert(0, ".")

_RECURRENCE_MIN = 3


def aggregate_harness_runs(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure aggregation over archive rows (no I/O).

    Returns: {total, status_counts, avg_turns_by_status, stall_histogram,
    issue_recurrence (class→count, sorted desc, ≥_RECURRENCE_MIN flagged),
    config_variants}.
    """
    rows = list(rows or [])
    status_counts: Counter = Counter()
    turns_by_status: Dict[str, List[int]] = {}
    stall_hist: Counter = Counter()
    issue_classes: Counter = Counter()
    config_variants: Counter = Counter()
    for r in rows:
        status = str(r.get("final_status") or "unknown")
        status_counts[status] += 1
        turns_by_status.setdefault(status, []).append(int(r.get("turns") or 0))
        stall_hist[int(r.get("verify_failures") or 0)] += 1
        # issue classes: the archive stores goal; issue text comes from the
        # decision JSON when present (column `issues_json` optional).
        for iss in (r.get("issues") or []):
            cls = str(iss)[:40].strip()
            if cls:
                issue_classes[cls] += 1
        try:
            cfg = json.loads(r.get("config_json") or "{}")
            key = tuple(sorted((k, str(v)) for k, v in cfg.items()))
        except Exception:
            key = ()
        config_variants[key] += 1
    avg_turns = {
        s: (sum(ts) / len(ts) if ts else 0.0) for s, ts in turns_by_status.items()
    }
    recurrent = [
        {"issue_class": c, "count": n, "harness_candidate": n >= _RECURRENCE_MIN}
        for c, n in issue_classes.most_common(20)
    ]
    return {
        "total": len(rows),
        "status_counts": dict(status_counts),
        "avg_turns_by_status": {s: round(v, 1) for s, v in avg_turns.items()},
        "stall_histogram": dict(sorted(stall_hist.items())),
        "issue_recurrence": recurrent,
        "config_variants": len(config_variants),
    }


def render_report(agg: Dict[str, Any], *, days: int) -> str:
    lines = [
        f"# Deathmatch Harness 复盘报告（近 {days} 天，report-only v1）",
        "",
        f"- 归档目标数: **{agg['total']}**",
        f"- 终态分布: {agg['status_counts']}",
        f"- 平均轮数: {agg['avg_turns_by_status']}",
        f"- 停滞直方图（verify_failures→频次）: {agg['stall_histogram']}",
        f"- 配置变体数: {agg['config_variants']}",
        "",
        "## 复发问题类（≥3 = harness 变更候选）",
        "",
        "| 问题类 | 次数 | 候选 |",
        "|---|---|---|",
    ]
    for it in agg["issue_recurrence"]:
        lines.append(f"| {it['issue_class']} | {it['count']} | {'✅' if it['harness_candidate'] else ''} |")
    if not agg["issue_recurrence"]:
        lines.append("| （无记录） | 0 |  |")
    lines += [
        "",
        "## 单组件有界变更提案（模板——人工填写，report-only 不自动改 harness）",
        "",
        "- 目标组件（五选一）: 提示词 / 工具编排 / 阈值配置 / 校验维度 / 反馈注入",
        "- 针对复发类: <从上方候选选 1 个>",
        "- 变更内容（单组件、有界、可回滚）: <…>",
        "",
        "## 验收门（AutoDesign 双集隔离 + JIT 三轴）",
        "",
        "1. 26 场景基线拆 train/dev **显式不相交**两集（评审记录隔离声明）",
        "2. 变更后: train 集判定准确率↑ **且** dev 集不降",
        "3. 三轴同报: reward / latency / cost——reward↑ 时 latency 或 cost 至少一项严格更优，其余不降",
        "4. 全程真实运行证据（exit-code 套件 + 必要时 Mode B 重跑）",
    ]
    return "\n".join(lines) + "\n"


async def _load_rows(days: int) -> List[Dict[str, Any]]:
    db_url = os.environ.get("CLEANUP_DB_URL")
    if not db_url:
        from app.core.config import get_config
        cfg = get_config()
        db_url = cfg.database_url
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    engine = create_async_engine(db_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        res = await s.execute(text(
            "SELECT conversation_id, final_status, turns, verify_failures, "
            "plan_steps, plan_done, judge_model, verify_model, config_json, goal, created_at, "
            "issues_json "
            "FROM deathmatch_harness_runs "
            f"WHERE created_at >= NOW() - INTERVAL '{int(days)} days' "
            "ORDER BY created_at DESC"
        ))
        rows = [dict(r._mapping) for r in res]
    await engine.dispose()
    # Unpack the recurrence axis (A4.9 W2-I1): issues_json → issues list.
    for r in rows:
        try:
            r["issues"] = json.loads(r.pop("issues_json", None) or "[]")
        except Exception:
            r["issues"] = []
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    rows = asyncio.run(_load_rows(args.days))
    agg = aggregate_harness_runs(rows)
    report = render_report(agg, days=args.days)
    out = args.out or os.path.join(
        os.path.dirname(__file__), "..", "..", "tests", "deathmatch_evals", "harness_report.md")
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(f"report written: {out} ({agg['total']} runs)")


if __name__ == "__main__":
    main()
