#!/usr/bin/env python3
"""P3.5 cache/observability report (plan 2026-10-18).

Reads persisted ``messages.context_info`` (prompt id/variant/hash + cache
counters) and aggregates cache read/write and prompt tokens.  This turns the
cache contract from an assumption into a queryable report.

Usage (from backend/):
  python -m scripts.prompt_cache_report --limit 5000
  python -m scripts.prompt_cache_report --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from typing import Any, Dict, List

sys.path.insert(0, ".")

from sqlalchemy import select


async def _rows(limit: int) -> List[str]:
    from app.db.database import AsyncSessionLocal, Message

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Message.context_info)
            .where(Message.context_info.is_not(None))
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        return [r[0] for r in result.all() if r[0]]


def _report(rows: List[str]) -> Dict[str, Any]:
    groups: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "messages": 0, "prompt_tokens": 0, "cache_read": 0,
        "cache_creation": 0, "cache_hit": 0, "cache_miss": 0,
        "cache_counted": 0,
    })
    legacy = 0
    cache_counted = 0
    for raw in rows:
        try:
            info = json.loads(raw)
        except Exception:
            continue
        if not isinstance(info, dict):
            continue
        if not info.get("prompt_id"):
            legacy += 1
        key = f"{info.get('prompt_id', '?')}:{info.get('prompt_variant', '?')}"
        g = groups[key]
        g["messages"] += 1
        g["prompt_tokens"] += int(info.get("tokens") or info.get("prompt_tokens") or 0)
        row_read = int(info.get("cache_read_input_tokens") or 0)
        row_creation = int(info.get("cache_creation_input_tokens") or 0)
        row_hit = int(info.get("prompt_cache_hit_tokens") or 0)
        row_miss = int(info.get("prompt_cache_miss_tokens") or 0)
        g["cache_read"] += row_read
        g["cache_creation"] += row_creation
        g["cache_hit"] += row_hit
        g["cache_miss"] += row_miss
        if row_hit or row_miss or row_read or row_creation:
            g["cache_counted"] += 1
            cache_counted += 1
    out = []
    for key, g in sorted(groups.items(), key=lambda kv: -kv[1]["messages"]):
        denom = g["cache_hit"] + g["cache_miss"]
        numerator = g["cache_hit"]
        if not denom:
            denom = g["cache_read"] + g["cache_creation"]
            numerator = g["cache_read"]
        rate = (numerator / denom) if denom else None
        out.append({"key": key, **g,
                    "hit_rate": round(rate, 3) if rate is not None else None})
    return {"messages_with_context": sum(g["messages"] for g in groups.values()),
            "legacy_messages": legacy,
            "cache_counted_messages": cache_counted,
            "groups": out}


def _interpreter_hint(exc: Exception) -> str:
    """Actionable hint when the script was started with the wrong interpreter.

    The project venv is the only supported runtime (``backend/requirements.txt``);
    a bare ``python3 -m scripts...`` picks up the system interpreter, which has
    no ``pgvector`` (imported transitively by ``app.db.database``) and fails as
    "ModuleNotFoundError: No module named 'pgvector'".
    """
    missing = getattr(exc, "name", "") or ""
    if isinstance(exc, ModuleNotFoundError) and missing in {"pgvector", "sqlalchemy", "asyncpg"}:
        return (
            f" (missing module '{missing}': this script must run with the project "
            "venv — `source .venv/bin/activate` then "
            "`python -m scripts.prompt_cache_report`; "
            "or call `.venv/bin/python -m scripts.prompt_cache_report`)"
        )
    return ""


async def main_async(limit: int, as_json: bool) -> int:
    try:
        rows = await _rows(limit)
    except Exception as exc:
        print(f"cache report failed to query DB: {type(exc).__name__}: {exc}"
              + _interpreter_hint(exc),
              file=sys.stderr)
        return 2
    data = _report(rows)
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"messages_with_context={data['messages_with_context']} "
              f"legacy_without_meta={data['legacy_messages']} "
              f"cache_counted={data['cache_counted_messages']}")
        for g in data["groups"]:
            rate = "n/a" if g["hit_rate"] is None else f"{g['hit_rate']:.1%}"
            print(f"{g['key']}: n={g['messages']} prompt_tokens={g['prompt_tokens']} "
                  f"cache_read={g['cache_read']} cache_creation={g['cache_creation']} "
                  f"cache_counted={g['cache_counted']} hit_rate={rate}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    return asyncio.run(main_async(args.limit, args.json))


if __name__ == "__main__":
    raise SystemExit(main())
