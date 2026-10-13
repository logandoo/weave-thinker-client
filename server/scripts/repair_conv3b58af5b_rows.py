"""One-off legacy data repair — conv 3b58af5b assistant rows (2026-08-23).

The 7 assistant messages persisted before the R1/R2/R3 display-regression fix
(f5089bd8) carry the broken shape:
  - display_sequence text-first with every tool card appended AFTER the
    answer (placeholder starvation, symptom ②);
  - results[] missing browser-derived citation sources (symptom ③);
  - dangling [N] markers in content / text surfaces.

This script plans and applies the deterministic repairs that are still
possible from the rows' own evidence:
  1. reorder display_sequence → all non-text items keep order, text items go
     last (chronologically the shipped answer followed its tool work);
  2. augment results[] from the row's own browser agent_steps (JSON pages +
     <tool-digest> envelopes), deduped against existing entries — the
     web_search prefix keeps ids 1..K untouched;
  3. run the production `_sanitize_cited_content` over content + text
     surfaces so unresolvable [N] markers are stripped exactly as the fixed
     pipeline would have done.

reasoning_content is NOT recoverable (the original reasoning stream was never
persisted) — recorded as unrecoverable, not silently skipped.

Usage (from backend/):
  python3 -m scripts.repair_conv3b58af5b_rows            # dry-run plan
  python3 -m scripts.repair_conv3b58af5b_rows --execute  # backup + apply

Config: [database] section of backend/config.toml.
"""
import argparse
import asyncio
import json
import sys
import time
import tomllib
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.api.chat import _extract_browser_sources, _sanitize_cited_content  # noqa: E402
from app.services.citation_ledger import normalize_url  # noqa: E402

DEFAULT_CONV_ID = "3b58af5b-a85b-4cfc-b304-63c64064dff7"


def _db_conf() -> dict:
    with open(BACKEND_DIR / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    db = cfg["database"]
    return {
        "host": db.get("host", "127.0.0.1"),
        "port": db.get("port", 5432),
        "user": db.get("username", "logan"),
        "password": db.get("password", ""),
        "dbname": db.get("name", "weave_thinker"),
    }


def reorder_display_sequence(seq: list[dict]):
    """Move text items to the end; everything else keeps relative order.
    Returns (new_seq, changed) — changed is decided by VALUE comparison of
    the resulting sequence, so an already-healthy row is a no-op."""
    texts = [i for i in seq if i.get("type") == "text"]
    others = [i for i in seq if i.get("type") != "text"]
    new_seq = others + texts
    return new_seq, new_seq != list(seq)


def augment_results(tr_obj: dict):
    """Append citation entries derived from the row's own browser agent_steps.
    Returns (new_results_list, added_count). web_search prefix ids unchanged."""
    results = tr_obj.get("results") if isinstance(tr_obj.get("results"), list) else []
    seen = set()
    for r in results:
        if isinstance(r, dict) and r.get("url"):
            seen.add(normalize_url(r["url"]))
    added = 0
    for step in tr_obj.get("agent_steps") or []:
        if not isinstance(step, dict):
            continue
        title = str(step.get("title") or "")
        name = str(step.get("name") or "")
        if step.get("step_type") != "tool":
            continue
        is_browser = ("浏览" in title) or name.startswith("browser") or title == "browser"
        if not is_browser:
            continue
        for url, src_title, snippet in _extract_browser_sources(str(step.get("content") or "")):
            key = normalize_url(url)
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "id": len(results) + 1,
                "title": (src_title or "")[:200],
                "url": url,
                "snippet": (snippet or "")[:300],
                "published_date": None,
            })
            added += 1
    return results, added


async def plan_row(content: str, tr_json: str | None):
    """Plan the repaired (content, tool_results_json) for one row.
    Returns (new_content, new_tr_json_or_None, plan_dict)."""
    plan = {"reordered": False, "results_added": 0, "sanitized": False}
    tr_obj = json.loads(tr_json) if tr_json else None
    if tr_obj is None:
        return content, None, plan

    new_seq, reordered = reorder_display_sequence(tr_obj.get("display_sequence") or [])
    plan["reordered"] = reordered
    tr_obj["display_sequence"] = new_seq

    new_results, added = augment_results(tr_obj)
    plan["results_added"] = added
    tr_obj["results"] = new_results

    new_tr_json = json.dumps(tr_obj, ensure_ascii=False)
    new_content, new_tr_json = await _sanitize_cited_content(content, new_tr_json)
    plan["sanitized"] = (new_content != content) or (new_tr_json != new_tr_json)

    changed = (
        reordered
        or added > 0
        or new_content != (content or "")
        or new_tr_json != (tr_json or "")
    )
    plan["changed"] = bool(changed)
    return new_content, new_tr_json, plan


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true",
                    help="apply the planned updates (default: dry-run)")
    ap.add_argument("--conv", default=DEFAULT_CONV_ID)
    args = ap.parse_args()

    import psycopg2
    conf = _db_conf()
    conn = psycopg2.connect(**conf)
    cur = conn.cursor(name="repair_legacy_rows")
    cur.execute(
        "SELECT id::text, content, COALESCE(tool_results,'') FROM messages "
        "WHERE conversation_id=%s AND role='assistant' ORDER BY created_at",
        (args.conv,),
    )
    rows = cur.fetchall()
    print(f"== repair plan for {len(rows)} assistant rows (conv={args.conv}) "
          f"mode={'EXECUTE' if args.execute else 'DRY-RUN'} ==")

    plans = []
    for row_id, content, tr_json in rows:
        new_content, new_tr_json, plan = asyncio.run(plan_row(content or "", tr_json or ""))
        status = "SKIP(no-op)" if not plan["changed"] else "UPDATE"
        print(f"[{status}] {row_id[:8]} reordered={plan['reordered']} "
              f"results_added={plan['results_added']} sanitized={plan['sanitized']}")
        plans.append((row_id, content or "", new_content, tr_json or "", new_tr_json, plan))

    to_apply = [p for p in plans if p[5]["changed"]]
    if not args.execute:
        print(f"dry-run: {len(to_apply)} row(s) would be updated; rerun with --execute")
        return 0

    if to_apply:
        backup_path = PROJECT_ROOT / "tests" / f"legacy_repair_backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
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
