#!/usr/bin/env python3
"""P2.5 production-failure feedback intake (plan 2026-10-18).

Turns an audit reject / negative feedback / tool error into a regression case.
Usage (from backend/):
  python -m scripts.prompt_feedback add --prompt-id audit.response \
      --source audit_reject --input "..." --expected reject --note "..."
  python -m scripts.prompt_feedback check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, ".")

CASES = Path(__file__).resolve().parents[1] / "tests" / "prompt_evals" / "regressions.json"


def _load() -> list[dict]:
    if not CASES.exists():
        return []
    return json.loads(CASES.read_text(encoding="utf-8"))


def _save(rows: list[dict]) -> None:
    CASES.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def add(args: argparse.Namespace) -> int:
    rows = _load()
    key = hashlib.sha256(
        f"{args.prompt_id}\0{args.source}\0{args.input}\0{args.expected}".encode("utf-8")
    ).hexdigest()[:16]
    if any(r.get("key") == key for r in rows):
        print(f"duplicate regression case ignored: {key}")
        return 0
    rows.append({
        "key": key,
        "id": f"reg-{date.today().isoformat()}-{key[:6]}",
        "prompt_id": args.prompt_id,
        "source": args.source,
        "input": args.input,
        "expected": args.expected,
        "must_contain": args.must_contain or [],
        "must_not_contain": args.must_not_contain or [],
        "note": args.note,
        "created": date.today().isoformat(),
    })
    _save(rows)
    print(f"added {len(rows)} regression case(s) -> {CASES}")
    return 0


def check() -> int:
    rows = _load()
    issues = []
    for row in rows:
        for field in ("key", "id", "prompt_id", "source", "input", "expected"):
            if not row.get(field):
                issues.append(f"{row.get('id') or '?'}: missing {field}")
    if issues:
        print("\n".join(issues))
        return 1
    print(f"regression cases: {len(rows)} valid")
    return 0


def intake(args: argparse.Namespace) -> int:
    """Merge the runtime intake queue into regressions.json (idempotent)."""
    from datetime import datetime, timedelta, timezone

    from app.services.prompt_feedback_intake import read_queue

    rows = _load()
    existing = {str(r.get("key")) for r in rows if r.get("key")}
    queue_file = Path(args.queue).expanduser() if args.queue else None
    events = read_queue(queue_file)
    cutoff = None
    if args.since_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(args.since_days))
    added = skipped = 0
    for event in events:
        key = str(event.get("key") or "")
        if not key or key in existing:
            skipped += 1
            continue
        if cutoff is not None:
            try:
                created = datetime.fromisoformat(str(event.get("created") or ""))
            except ValueError:
                created = None
            if created is not None and created < cutoff:
                skipped += 1
                continue
        rows.append({
            "key": key,
            "kind": "intake",
            "id": f"reg-{date.today().isoformat()}-{key[:6]}",
            "prompt_id": event.get("prompt_id", ""),
            "source": event.get("source", ""),
            "input": event.get("input", ""),
            "expected": event.get("expected", ""),
            "must_contain": event.get("must_contain") or [],
            "must_not_contain": event.get("must_not_contain") or [],
            "note": event.get("note", ""),
            "created": date.today().isoformat(),
        })
        existing.add(key)
        added += 1
    apply = bool(getattr(args, "apply", False)) and not getattr(args, "dry_run", False)
    if added and apply:
        _save(rows)
    print(f"intake: queue={queue_file or 'default'} events={len(events)} "
          f"added={added} skipped={skipped} total={len(rows)} "
          f"{'applied' if apply else 'dry-run (pass --apply to write)'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    a = sub.add_parser("add")
    a.add_argument("--prompt-id", required=True)
    a.add_argument("--source", required=True,
                   choices=("audit_reject", "user_feedback", "tool_error", "citation_failure"))
    a.add_argument("--input", required=True)
    a.add_argument("--expected", required=True)
    a.add_argument("--note", default="")
    a.add_argument("--must-contain", action="append", default=[])
    a.add_argument("--must-not-contain", action="append", default=[])
    sub.add_parser("check")
    i = sub.add_parser("intake")
    i.add_argument("--queue", default="", help="override queue JSONL path")
    i.add_argument("--since-days", type=int, default=0,
                   help="only merge events newer than N days")
    i.add_argument("--apply", action="store_true",
                   help="write the merged rows (default is dry-run)")
    i.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.command == "add":
        return add(args)
    if args.command == "intake":
        return intake(args)
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
