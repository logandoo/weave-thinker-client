#!/usr/bin/env python3
"""C-01/C-02 refactor p95 baseline (2026-10-19).

Runs a fixed, in-memory behavioural suite that exercises the agent loop and
the chat SSE transport, then reports p50/p95 of per-test CALL durations plus
structural metrics for the target files.  It is a regression baseline for the
move-only split (not production traffic p95).

Usage (from the repo root):
  python3 backend/scripts/refactor_p95_benchmark.py --label current
  python3 backend/scripts/refactor_p95_benchmark.py --root /tmp/wt_baseline --label baseline
  python3 backend/scripts/refactor_p95_benchmark.py --compare baseline.json current.json
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SUITE = (
    "tests/test_interjection.py",
    "tests/test_agent_loop_guardrail_tail.py",
    "tests/test_stream_persistence.py",
    "tests/test_output_unit_boundaries.py",
    "tests/test_deathmatch_autonomy.py",
    "tests/test_title_closure_capture.py",
    "tests/test_server_drain.py",
    "tests/test_chunked_claim_audit.py",
    "tests/test_audit_evidence_window.py",
    "tests/test_integrity_truncation_fixes.py",
)

TARGETS = (
    "app/services/agent_loop.py",
    "app/services/agent_loop_orchestrator.py",
    "app/api/chat.py",
    "app/api/chat_stream_transport.py",
    "app/services/deathmatch_service.py",
    "app/services/voice_service.py",
)

_DURATION = re.compile(r"^\s*([\d.]+)s\s+(call|setup|teardown)\s+(\S+)")


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    # nearest-rank p95 over the FULL test population (no rounding fudge)
    idx = min(len(ordered) - 1, max(0, __import__("math").ceil(0.95 * len(ordered)) - 1))
    return ordered[idx]


def _junit_durations(path: Path) -> list[tuple[float, str]]:
    import xml.etree.ElementTree as ET

    out: list[tuple[float, str]] = []
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return out
    for case in root.iter("testcase"):
        try:
            out.append((float(case.get("time") or 0.0),
                        f"{case.get('classname') or ''}::{case.get('name') or ''}"))
        except ValueError:
            continue
    return out


def run(root: Path, label: str, code_ref: str = "") -> dict:
    import time
    import tempfile

    backend = root / "backend"
    junit = Path(tempfile.gettempdir()) / f"wt_p95_{label}.xml"
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *SUITE, "-q", "--durations=0",
         f"--junitxml={junit}", "-p", "no:cacheprovider"],
        cwd=backend, capture_output=True, text=True,
    )
    wall = time.perf_counter() - started
    cases = [{"seconds": round(sec, 4), "test": name}
             for sec, name in _junit_durations(junit)]
    duration_source = "junit"
    if not cases:
        duration_source = "pytest-durations-fallback"
        for line in proc.stdout.splitlines():
            m = _DURATION.match(line)
            if m and m.group(2) == "call":
                cases.append({"seconds": float(m.group(1)), "test": m.group(3)})
    durations = [c["seconds"] for c in cases]
    summary = ""
    for line in reversed(proc.stdout.splitlines()):
        if " passed" in line or " failed" in line:
            summary = line.strip()
            break
    files = {}
    for rel in TARGETS:
        path = backend / rel
        try:
            text = path.read_text(encoding="utf-8")
            files[rel] = {"lines": text.count("\n") + 1, "bytes": len(text)}
        except OSError:
            files[rel] = {"lines": 0, "bytes": 0}
    return {
        "label": label,
        "code_ref": code_ref,
        "root": str(root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pytest_exit": proc.returncode,
        "pytest_summary": summary,
        "duration_source": duration_source,
        "wall_seconds": round(wall, 3),
        "call_count": len(durations),
        "p50_seconds": round(statistics.median(durations), 4) if durations else 0.0,
        "p95_seconds": round(_p95(durations), 4),
        "max_seconds": round(max(durations), 4) if durations else 0.0,
        "slowest": sorted(cases, key=lambda c: -c["seconds"])[:10],
        "durations": sorted(durations),
        "files": files,
    }


def compare(baseline_path: str, current_path: str, threshold: float) -> int:
    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    current = json.loads(Path(current_path).read_text(encoding="utf-8"))
    base_p95 = float(baseline.get("p95_seconds") or 0.0)
    cur_p95 = float(current.get("p95_seconds") or 0.0)
    base_max = float(baseline.get("max_seconds") or 0.0)
    cur_max = float(current.get("max_seconds") or 0.0)
    delta = ((cur_p95 - base_p95) / base_p95 * 100.0) if base_p95 else 0.0
    max_delta = ((cur_max - base_max) / base_max * 100.0) if base_max else 0.0
    base_valid = (baseline.get("pytest_exit") == 0
                  and baseline.get("call_count", 0) > 0
                  and base_p95 > 0
                  and baseline.get("duration_source") == "junit")
    cur_valid = (current.get("pytest_exit") == 0
                 and current.get("call_count", 0) > 0
                 and cur_p95 > 0
                 and current.get("duration_source") == "junit")
    ok = (base_valid and cur_valid and delta <= threshold
          and max_delta <= 20.0)
    print(json.dumps({
        "baseline": baseline_path, "current": current_path,
        "baseline_p95": base_p95, "current_p95": cur_p95,
        "delta_percent": round(delta, 2), "threshold_percent": threshold,
        "baseline_max": base_max, "current_max": cur_max,
        "max_delta_percent": round(max_delta, 2),
        "baseline_wall": baseline.get("wall_seconds"),
        "current_wall": current.get("wall_seconds"),
        "baseline_valid": base_valid, "current_valid": cur_valid,
        "baseline_call_count": baseline.get("call_count"),
        "current_call_count": current.get("call_count"),
        "duration_source": current.get("duration_source"),
        "ok": ok,
        "baseline_summary": baseline.get("pytest_summary"),
        "current_summary": current.get("pytest_summary"),
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--label", default="current")
    ap.add_argument("--ref", default="",
                    help="code ref measured (e.g. 883ac883 or HEAD)")
    ap.add_argument("--out", default="")
    ap.add_argument("--compare", nargs=2, default=None,
                    metavar=("BASELINE", "CURRENT"))
    ap.add_argument("--threshold", type=float, default=5.0)
    args = ap.parse_args()
    if args.compare:
        return compare(args.compare[0], args.compare[1], args.threshold)
    data = run(Path(args.root).resolve(), args.label, args.ref)
    out = Path(args.out) if args.out else (
        Path(__file__).resolve().parents[1] / "tests"
        / f"refactor_p95_{args.label}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(json.dumps({k: data[k] for k in (
        "label", "pytest_exit", "pytest_summary", "call_count",
        "p50_seconds", "p95_seconds", "max_seconds", "files")},
        ensure_ascii=False, indent=2))
    return 0 if data["pytest_exit"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
