"""One-off legacy data repair — conv a040c24e citation misalignment (2026-09-10).

Production evidence (<cloud-host> DB, investigated 2026-09-10): message
baab73dc-ff9d-4909-acec-1c788d0a36e2 (assistant, 2026-09-06 00:21 local)
contains "[1][11]"×2 + "[3][11]" + "[3][11][7]" in its answer. The model
reused the citation numbers of an EARLIER turn (fd417997, whose ledger had
[1]=smzdm "7999的Pro是素车" article and [11]=zeeho 配件价格查询 — the actual
sources of the 素车/选装 claims). This turn's own results[] has only 10
entries, so:
  - [11] is out of range → the frontend renders no citation card (dangling);
  - [1]  resolves positionally to this turn's results[0] (电摩之家 M3 100 —
    completely unrelated to the AE6 Pro 素车 claim).

Root cause (fixed in code this wave): historical [N] markers and numbered
search lists replayed verbatim into later turns' context.

The deterministic repair possible from the row's own evidence (precedent:
scripts/repair_conv3b58af5b_rows.py — strip dangling markers so every cited
[N] resolves):
  1. remove the joint marker "[1][11]" (2 occurrences — both hang on the
     素车/选装 assertions; neither id resolves to the intended source here);
  2. remove the remaining "[11]" occurrences ("[3][11]"→"[3]",
     "[3][11][7]"→"[3][7]"),
applied to content + display_sequence text items + content_segments.
After repair every cited [N] in the row resolves to results[] (zero dangling).

Also provides --scan-all: read-only report of every assistant row in the DB
whose cited [N] exceeds its results[] length (candidate rows with the same
cross-turn corruption class). Report only — never auto-fixed, because
enumeration-vs-citation disambiguation requires the LLM judge (⛔ no
mechanical classifier).

Usage (from backend/):
  python3 -m scripts.repair_conv_a040c24e_citations             # dry-run plan
  python3 -m scripts.repair_conv_a040c24e_citations --execute   # backup + apply
  python3 -m scripts.repair_conv_a040c24e_citations --scan-all  # DB-wide report

Config: [database] section of backend/config.toml.
"""
import argparse
import json
import sys
import time
import tomllib
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.services.citation_ledger import _CITE_RE, _FENCE_RE  # noqa: E402

DEFAULT_CONV_ID = "a040c24e-8101-4c27-9fbc-66c0f8bbd79a"
# Only this row is repaired: its [11] markers are PROVABLY dangling (10
# results) and its [1] provably misresolves. The sibling row c8562c6e also
# contains "[1][11]" but its own ledger HAS 20 entries with [11]=58moto
# "8千价位电摩二选一" — a correct self-turn citation; the blanket transform
# would strip legitimate citations there (dry-run against production caught
# this, 2026-09-10). Its single in-range [1] over-citation is not
# mechanically provable → left untouched (documented).
DEFAULT_MSG_ID = "baab73dc-ff9d-4909-acec-1c788d0a36e2"

# The documented corruption markers for THIS row (see module docstring).
_JOINT_MARKER = "[1][11]"
_DANGLING_MARKER = "[11]"


def _db_conf() -> dict:
    with open(BACKEND_DIR / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    db = cfg["database"]
    # Env overrides let a one-off run target another deployment's DB without
    # editing config.toml or committing credentials (deploy_60.sh convention).
    import os
    return {
        "host": os.environ.get("WEAVE_DB_HOST") or db.get("host", "127.0.0.1"),
        "port": int(os.environ.get("WEAVE_DB_PORT") or db.get("port", 5432)),
        "user": os.environ.get("WEAVE_DB_USER") or db.get("username", "logan"),
        "password": os.environ.get("WEAVE_DB_PASSWORD") or db.get("password", ""),
        "dbname": os.environ.get("WEAVE_DB_NAME") or db.get("name", "weave_thinker"),
    }


def _strip_documented_markers(text: str):
    """Remove "[1][11]" first (joint), then remaining "[11]" (dangling).
    Order matters: after the joint removal no "[1]" remains in this row, and
    "[3][11]"/"[3][11][7]" degrade to "[3]"/"[3][7]". Returns (text, j, d)."""
    joint = text.count(_JOINT_MARKER)
    t = text.replace(_JOINT_MARKER, "")
    dangling = t.count(_DANGLING_MARKER)
    t = t.replace(_DANGLING_MARKER, "")
    return t, joint, dangling


def plan_row(content: str, tr_json: str | None):
    """Plan the repaired (content, tool_results_json) for one row.
    Pure function — no I/O. plan counts track the primary content surface;
    surfaces_fixed counts every text surface touched."""
    plan = {"removed_joint": 0, "removed_dangling": 0, "surfaces_fixed": 0, "changed": False}
    new_content, j, d = _strip_documented_markers(content or "")
    plan["removed_joint"] += j
    plan["removed_dangling"] += d
    if j or d:
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

    def _fix_surface(text):
        nt, j2, d2 = _strip_documented_markers(text)
        if j2 or d2:
            plan["surfaces_fixed"] += 1
        return nt

    for item in tr.get("display_sequence") or []:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("content"), str):
            item["content"] = _fix_surface(item["content"])
    segs = tr.get("content_segments")
    if isinstance(segs, list):
        for i, seg in enumerate(segs):
            if isinstance(seg, str):
                segs[i] = _fix_surface(seg)
            elif isinstance(seg, dict) and isinstance(seg.get("text"), str):
                seg["text"] = _fix_surface(seg["text"])

    plan["changed"] = bool(plan["surfaces_fixed"])
    new_tr_json = json.dumps(tr, ensure_ascii=False) if plan["changed"] else tr_json
    return new_content, new_tr_json, plan


def scan_row(content: str, tr_json: str | None) -> set:
    """Return the set of cited [N] that cannot resolve to results[] (out of
    range). Mechanical only — in-range semantic misdirection is NOT
    detectable here. Full-width （N） enumerations never match."""
    try:
        tr = json.loads(tr_json) if tr_json else {}
    except (json.JSONDecodeError, TypeError):
        tr = {}
    if not isinstance(tr, dict):
        tr = {}
    results = tr.get("results") if isinstance(tr.get("results"), list) else []
    n = len(results)
    if n == 0:
        return set()
    prose = _FENCE_RE.sub("", content or "")
    return {int(m) for m in _CITE_RE.findall(prose) if not (1 <= int(m) <= n)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true",
                    help="apply the planned updates (default: dry-run)")
    ap.add_argument("--msg", default=DEFAULT_MSG_ID,
                    help="message id to repair (default: the documented baab73dc row)")
    ap.add_argument("--force", action="store_true",
                    help="required when --msg targets a non-documented row (A4.9 R1 M6: "
                         "the transform is hardcoded to baab73dc's corruption and would "
                         "strip legitimate self-turn citations elsewhere)")
    ap.add_argument("--scan-all", action="store_true",
                    help="read-only DB-wide report of dangling-citation rows")
    args = ap.parse_args()

    if args.msg != DEFAULT_MSG_ID and not args.force:
        print("refused: --msg targets a non-documented row; the marker transform is "
              "hardcoded to baab73dc's documented corruption and would strip legitimate "
              "citations elsewhere. Inspect the row first, then rerun with --force.")
        return 2

    import psycopg2
    conn = psycopg2.connect(**_db_conf())

    if args.scan_all:
        cur = conn.cursor(name="scan_dangling")
        cur.execute(
            "SELECT id::text, conversation_id::text, content, COALESCE(tool_results,'') "
            "FROM messages WHERE role='assistant' AND tool_results IS NOT NULL "
            "ORDER BY created_at"
        )
        flagged = 0
        total = 0
        for row_id, conv_id, content, tr_json in cur:
            total += 1
            dangling = scan_row(content or "", tr_json or "")
            if dangling:
                flagged += 1
                print(f"[DANGLING] conv={conv_id[:8]} msg={row_id[:8]} ids={sorted(dangling)}")
        print(f"scan-all: {flagged}/{total} assistant rows with out-of-range [N] (report only)")
        conn.close()
        return 0

    cur = conn.cursor(name="repair_a040c24e")
    cur.execute(
        "SELECT id::text, content, COALESCE(tool_results,'') FROM messages "
        "WHERE id=%s AND role='assistant'",
        (args.msg,),
    )
    rows = cur.fetchall()
    print(f"== citation repair plan for {len(rows)} assistant row(s) (msg={args.msg}) "
          f"mode={'EXECUTE' if args.execute else 'DRY-RUN'} ==")

    plans = []
    for row_id, content, tr_json in rows:
        new_content, new_tr_json, plan = plan_row(content or "", tr_json or "")
        dangling_after = scan_row(new_content, new_tr_json)
        status = "SKIP(no-op)" if not plan["changed"] else "UPDATE"
        print(f"[{status}] {row_id[:8]} joint={plan['removed_joint']} "
              f"dangling={plan['removed_dangling']} surfaces={plan['surfaces_fixed']} "
              f"dangling_after={sorted(dangling_after)}")
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
