"""Offline TMA-NM provenance audit (NeuroTaint-style source -> sink, read-only).

Builds a small provenance graph from ``artifact_taint`` (P1d) and
``memory_verdicts`` (P1a-P2) and reports places where an untrusted source
reached a consequential action, where a derived artifact looks laundered
(``agent``/``trusted`` taint with an untrusted parent), and where authority has
no provenance parents at all.

This is an *offline auditor*, not a runtime gate: the act gate stays the
authority.  Nothing is mutated; dry-run is the only mode.

Usage (from backend/):
    python -m scripts.audit_memory_provenance
    python -m scripts.audit_memory_provenance --tenant <segment> --days 7
    python -m scripts.audit_memory_provenance --output ../tests/dcpg_report.md
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from typing import Any, Iterable

FINDING_KINDS = (
    "untrusted_act_allow",
    "laundering_suspect",
    "trusted_without_parents",
    "untrusted_without_parents",
)


def _parents_of(row: dict[str, Any]) -> list[str]:
    raw = row.get("parent_ids") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if str(x or "").strip()]


def build_graph(
    artifacts: Iterable[dict[str, Any]],
    verdicts: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    edges: list[tuple[str, str]] = []
    for row in artifacts or []:
        node = {
            "user_id": str(row.get("user_id") or ""),
            "artifact_type": str(row.get("artifact_type") or ""),
            "artifact_id": str(row.get("artifact_id") or ""),
            "taint": str(row.get("taint") or "untrusted"),
            "origin": str(row.get("origin") or "legacy_untrusted"),
            "parent_ids": _parents_of(row),
            "tenant_id": str(row.get("tenant_id") or ""),
        }
        nodes.append(node)
        by_id[node["artifact_id"]] = node
        for parent in node["parent_ids"]:
            edges.append((parent, node["artifact_id"]))
    return {
        "artifacts": nodes,
        "by_id": by_id,
        "edges": edges,
        "verdicts": [
            {
                "tenant_id": str(v.get("tenant_id") or ""),
                "user_id": str(v.get("user_id") or ""),
                "conversation_id": str(v.get("conversation_id") or ""),
                "tool_name": str(v.get("tool_name") or ""),
                "decision": str(v.get("decision") or ""),
                "taint": str(v.get("taint") or "untrusted"),
                "blocked": bool(v.get("blocked")),
                "mode": str(v.get("mode") or "shadow"),
                "reason": str(v.get("reason") or ""),
            }
            for v in verdicts or []
        ],
    }


def find_findings(graph: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = graph.get("artifacts") or []
    verdicts = graph.get("verdicts") or []
    by_id = graph.get("by_id") or {}

    untrusted_allow = [
        v for v in verdicts
        if v.get("decision") == "allow"
        and v.get("taint") == "untrusted"
        and not v.get("blocked")
    ]
    laundering: list[dict[str, Any]] = []
    trusted_orphans: list[dict[str, Any]] = []
    untrusted_orphans: list[dict[str, Any]] = []
    for node in nodes:
        parents = node.get("parent_ids") or []
        if node.get("taint") in ("agent", "trusted"):
            bad = [
                p for p in parents
                if str((by_id.get(p) or {}).get("taint") or "") == "untrusted"
            ]
            if bad:
                laundering.append({**node, "untrusted_parents": bad})
        if not parents and node.get("taint") == "trusted":
            trusted_orphans.append(node)
        if not parents and node.get("taint") == "untrusted":
            untrusted_orphans.append(node)

    findings = [
        {
            "kind": "untrusted_act_allow",
            "severity": "critical",
            "count": len(untrusted_allow),
            "detail": (
                "allow verdicts with untrusted taint (shadow-mode evidence of "
                "unsafe consequential actions; enforce should turn these into deny)"
            ),
            "examples": untrusted_allow[:5],
        },
        {
            "kind": "laundering_suspect",
            "severity": "critical",
            "count": len(laundering),
            "detail": (
                "derived artifacts with agent/trusted taint whose parents are untrusted"
            ),
            "examples": laundering[:5],
        },
        {
            "kind": "trusted_without_parents",
            "severity": "medium",
            "count": len(trusted_orphans),
            "detail": "trusted artifacts without provenance parents",
            "examples": trusted_orphans[:5],
        },
        {
            "kind": "untrusted_without_parents",
            "severity": "low",
            "count": len(untrusted_orphans),
            "detail": "untrusted artifacts without provenance parents (audit gap)",
            "examples": untrusted_orphans[:5],
        },
    ]
    return findings


def _decision_counts(verdicts: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in verdicts:
        key = str(row.get("decision") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    counts["blocked"] = sum(1 for row in verdicts if row.get("blocked"))
    return counts


def render_report(
    graph: dict[str, Any],
    findings: list[dict[str, Any]],
    *,
    tenant: str = "",
    since_days: int | None = None,
) -> str:
    by_kind = {f["kind"]: f for f in findings}
    lines = [
        "# Memory provenance audit (DCPG)",
        "",
        f"- tenant: `{tenant or '(all)'}`",
        f"- window: {'last %sd' % since_days if since_days else 'all rows'}",
        f"- artifacts: {len(graph.get('artifacts') or [])}",
        f"- verdicts: {len(graph.get('verdicts') or [])}",
        f"- edges: {len(graph.get('edges') or [])}",
        f"- generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Findings",
        "",
        "| kind | severity | count |",
        "|---|---|---|",
    ]
    for kind in FINDING_KINDS:
        finding = by_kind.get(kind) or {
            "kind": kind, "severity": "-", "count": 0, "detail": "",
        }
        lines.append(
            f"| {finding['kind']} | {finding['severity']} | {finding['count']} |"
        )
    for kind in FINDING_KINDS:
        finding = by_kind.get(kind)
        if not finding or not finding.get("count"):
            continue
        lines += ["", f"### {kind}", "", finding.get("detail", ""), ""]
        for example in (finding.get("examples") or [])[:5]:
            lines.append(f"- `{json.dumps(example, ensure_ascii=False, default=str)[:400]}`")
    lines += ["", "## Verdict decisions", "", "```json"]
    lines.append(json.dumps(_decision_counts(graph.get("verdicts") or []), ensure_ascii=False))
    lines += ["```", ""]
    return "\n".join(lines)


async def _load(
    tenant: str, days: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from sqlalchemy import text
    from app.db.database import AsyncSessionLocal

    where = []
    params: dict[str, Any] = {}
    if tenant:
        where.append("tenant_id = :tenant")
        params["tenant"] = tenant
    artifact_sql = (
        "SELECT user_id, artifact_type, artifact_id, tenant_id, taint, origin, "
        "parent_ids FROM artifact_taint"
    )
    verdict_sql = (
        "SELECT tenant_id, user_id, conversation_id, tool_name, decision, taint, "
        "blocked, mode, reason FROM memory_verdicts"
    )
    if where:
        artifact_sql += " WHERE " + " AND ".join(where)
        verdict_sql += " WHERE " + " AND ".join(where)
    if days:
        cutoff = int((datetime.utcnow() - timedelta(days=days)).timestamp())
        verdict_sql += (" AND " if where else " WHERE ") + "created_at_epoch >= :cutoff"
        params["cutoff"] = cutoff
    async with AsyncSessionLocal() as db:
        artifacts = [
            dict(row._mapping)
            for row in (await db.execute(text(artifact_sql), params)).fetchall()
        ]
        verdicts = [
            dict(row._mapping)
            for row in (await db.execute(text(verdict_sql), params)).fetchall()
        ]
    return artifacts, verdicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="")
    parser.add_argument("--days", type=int, default=0)
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    try:
        import asyncio

        artifacts, verdicts = asyncio.run(
            _load(args.tenant, args.days or None)
        )
    except Exception as exc:  # pragma: no cover - operator-facing guard
        print(f"audit failed: {exc}", file=sys.stderr)
        return 1
    graph = build_graph(artifacts, verdicts)
    findings = find_findings(graph)
    report = render_report(
        graph, findings, tenant=args.tenant, since_days=args.days or None,
    )
    if args.output:
        from pathlib import Path
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"audit report written: {args.output}")
    else:
        print(report)
    critical = sum(
        1 for f in findings if f["severity"] == "critical" and f["count"]
    )
    return 0 if critical == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
