"""One-off legacy data repair — dangling citation rows (2026-09-10 wave-2).

The DB-wide scan (scripts/repair_conv_a040c24e_citations.py --scan-all)
flagged 8/4046 assistant rows with out-of-range [N] markers. Row baab73dc
(conv a040c24e) is handled by that script. This script carries the per-row
rulings for the remaining 7, each verified by reading the marker contexts
and bibliography sections on 2026-09-10:

REPAIR (citation-form markers that resolve to NOTHING — id > results_n AND
no matching entry in the row's own 参考文献 section):
  - e6b9e64c (灭螺效果评价方法): strip {11} only. Its 参考文献 lists [12]
    ("2025年智能农业机械市场研究报告") — in-text [12] is INTERNALLY
    consistent → kept. [11] has no bibliography entry → stripped.
  - d24f99c7 (same conv): strip {13}. Bibliography holds [1]-[10] only.
  - 1803b4ee (ai max+ 395上): strip {11,12,13,14,15}. No bibliography;
    all citation-form (GitHub issue references) — cross-turn stale reuse.
  - 9b175329 (订单簿DNA): strip {26}. No bibliography; academic citation form.
  - 49a7dfc4 (AI与ATS语言): strip {19}. No bibliography; citation form.

DO NOT TOUCH (rulings):
  - f1407235: [0] is `s.getsockname()[0]` — a Python tuple index in UNFENCED
    code, not a citation. Stripping would mangle code.
  - 5d9f2747: the [12]-[28] markers are DATA inside the user's own
    citation-misalignment ANALYSIS table (正文编号 vs 参考文献编号, 匹配/错位
    columns). Stripping would destroy the analytical content.

Transform: fence-masked structural removal via
CitationLedger._sanitize_remove_ids over content + display_sequence text
items + content_segments (same surfaces as the production sanitizer).
dry-run default; --execute writes a backup to tests/ first; idempotent.

Usage (from backend/):
  python3 -m scripts.repair_dangling_citation_rows_20260910            # dry-run
  python3 -m scripts.repair_dangling_citation_rows_20260910 --execute  # backup + apply

Config: [database] of backend/config.toml; WEAVE_DB_* env overrides.
"""
import argparse
import json
import os
import sys
import time
import tomllib
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.services.citation_ledger import CitationLedger  # noqa: E402

# message id -> set of PROVABLY-unresolvable citation ids to strip (see docstring)
REPAIR_MAP = {
    "e6b9e64c-ba62-4a6b-85fc-8c6073747e6a": {11},
    "d24f99c7-b8cd-456d-bae5-17ef7c4dd1c2": {13},
    "1803b4ee-5158-4ea3-b044-7f46ec2c3dcc": {11, 12, 13, 14, 15},
    "9b175329-9d64-497d-86ad-f052949503c6": {26},
    "49a7dfc4-afe2-4e0b-b2aa-782d6d0c932e": {19},
}


def _db_conf() -> dict:
    with open(BACKEND_DIR / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    db = cfg["database"]
    return {
        "host": os.environ.get("WEAVE_DB_HOST") or db.get("host", "127.0.0.1"),
        "port": int(os.environ.get("WEAVE_DB_PORT") or db.get("port", 5432)),
        "user": os.environ.get("WEAVE_DB_USER") or db.get("username", "logan"),
        "password": os.environ.get("WEAVE_DB_PASSWORD") or db.get("password", ""),
        "dbname": os.environ.get("WEAVE_DB_NAME") or db.get("name", "weave_thinker"),
    }


def plan_row(content: str, tr_json: str | None, strip_ids: set):
    """Plan the repair for one row: strip strip_ids from content + text
    surfaces. Pure function. Returns (new_content, new_tr_json, plan)."""
    ledger = CitationLedger()  # only its _sanitize_remove_ids is used
    plan = {"strip_ids": sorted(strip_ids), "surfaces_fixed": 0, "changed": False}
    new_content = ledger._sanitize_remove_ids(content or "", strip_ids)
    if new_content != (content or ""):
        plan["surfaces_fixed"] += 1
    if not tr_json:
        plan["changed"] = bool(plan["surfaces_fixed"])
        return new_content, tr_json, plan
    try:
        tr = json.loads(tr_json)
    except (json.JSONDecodeError, TypeError):
        plan["changed"] = bool(plan["surfaces_fixed"])
        return new_content, tr_json, plan
    if not isinstance(tr, dict):
        plan["changed"] = bool(plan["surfaces_fixed"])
        return new_content, tr_json, plan

    def _fix(text):
        nt = ledger._sanitize_remove_ids(text, strip_ids)
        if nt != text:
            plan["surfaces_fixed"] += 1
        return nt

    for item in tr.get("display_sequence") or []:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("content"), str):
            item["content"] = _fix(item["content"])
    segs = tr.get("content_segments")
    if isinstance(segs, list):
        for i, seg in enumerate(segs):
            if isinstance(seg, str):
                segs[i] = _fix(seg)
            elif isinstance(seg, dict) and isinstance(seg.get("text"), str):
                seg["text"] = _fix(seg["text"])

    plan["changed"] = bool(plan["surfaces_fixed"])
    new_tr_json = json.dumps(tr, ensure_ascii=False) if plan["changed"] else tr_json
    return new_content, new_tr_json, plan


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true",
                    help="apply the planned updates (default: dry-run)")
    args = ap.parse_args()

    import psycopg2
    conn = psycopg2.connect(**_db_conf())
    cur = conn.cursor(name="repair_dangling_20260910")
    cur.execute(
        "SELECT id::text, content, COALESCE(tool_results,'') FROM messages WHERE id = ANY(%s)",
        (list(REPAIR_MAP.keys()),),
    )
    rows = cur.fetchall()
    print(f"== dangling-citation repair plan for {len(rows)} row(s) "
          f"mode={'EXECUTE' if args.execute else 'DRY-RUN'} ==")

    plans = []
    for row_id, content, tr_json in rows:
        strip_ids = REPAIR_MAP[row_id]
        new_content, new_tr_json, plan = plan_row(content or "", tr_json or "", strip_ids)
        leftover = {n for n in strip_ids if f"[{n}]" in new_content}
        status = "SKIP(no-op)" if not plan["changed"] else "UPDATE"
        print(f"[{status}] {row_id[:8]} strip={sorted(strip_ids)} "
              f"surfaces={plan['surfaces_fixed']} leftover={sorted(leftover)}")
        plans.append((row_id, content or "", new_content, tr_json or "", new_tr_json, plan))

    to_apply = [p for p in plans if p[5]["changed"]]
    if not args.execute:
        print(f"dry-run: {len(to_apply)} row(s) would be updated; rerun with --execute")
        return 0

    if to_apply:
        backup_path = PROJECT_ROOT / "tests" / f"legacy_repair_backup_dangling_{time.strftime('%Y%m%d_%H%M%S')}.json"
        payload = [
            {"id": rid, "content": old_c, "tool_results": old_tr}
            for rid, old_c, _, old_tr, _, _ in to_apply
        ]
        backup_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        print(f"backup written: {backup_path}")

    applied = 0
    with conn:
        with conn.cursor() as wcur:
            for rid, _, new_c, _, new_tr, _ in to_apply:
                wcur.execute(
                    "UPDATE messages SET content=%s, tool_results=%s WHERE id=%s",
                    (new_c, new_tr, rid),
                )
                applied += wcur.rowcount
    conn.close()
    print(f"applied: {applied} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
