"""Offline fixed-FPR evaluation for the P3 injection detector.

Reports benign false-positive rate at a target budget (default 1%), attack
recall, over-defense, and per-bucket recall on the own-tool-output corpus.
The structural detector is a **prefilter**; this report is evidence about
text, not an authority decision.

Usage (from backend/):
    python -m scripts.eval_injection_detectors
    python -m scripts.eval_injection_detectors --target-fpr 0.01 \
        --output ../tests/injection_eval_report.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def build_report(report: dict[str, Any], corpus_path: str) -> str:
    lines = [
        "# Injection detector evaluation (fixed-FPR)",
        "",
        f"- corpus: `{corpus_path}`",
        f"- detector: `{report['detector']}` (authority: **prefilter**)",
        f"- target FPR: {report['target_fpr']:.2%}",
        f"- calibrated threshold: {report['threshold']}",
        f"- runtime threshold: {report.get('runtime_threshold')} "
        "(what `detect()` actually uses)",
        f"- benign items: {report['benign']}",
        f"- attack items: {report['attacks']}",
        "",
        "## Operating point",
        "",
        "| metric | value |",
        "|---|---|",
        f"| benign FPR | {report['fpr']:.2%} ({report['false_positives']}/{report['benign']}) |",
        f"| attack recall | {report['recall']:.2%} |",
        f"| over-defense buckets | {', '.join(report['over_defense']) or 'none'} |",
        "",
        "## Recall by bucket",
        "",
        "| bucket | attacks | hits | recall |",
        "|---|---|---|---|",
    ]
    for bucket, row in sorted(report.get("buckets", {}).items()):
        lines.append(
            f"| {bucket} | {row['attacks']} | {row['hits']} | {row['recall']:.2%} |"
        )
    lines += [
        "",
        "## Recall by language",
        "",
        "| language | attacks | hits | recall |",
        "|---|---|---|---|",
    ]
    for bucket, row in sorted(report.get("language_buckets", {}).items()):
        lines.append(
            f"| {bucket} | {row['attacks']} | {row['hits']} | {row['recall']:.2%} |"
        )
    lines += [
        "",
        "## Benign over-defense by tool/channel",
        "",
        "| tool/channel | items | false positives | FPR |",
        "|---|---|---|---|",
    ]
    for bucket, row in sorted(report.get("benign_by_tool", {}).items()):
        lines.append(
            f"| {bucket} | {row['items']} | {row['hits']} | {row['fpr']:.2%} |"
        )
    external = report.get("external") or {}
    lines += [
        "",
        "## External baseline",
        "",
        (
            f"- configured: `{external.get('available')}`"
            + (f" ({external.get('reason')})" if external.get("reason") else "")
            + " — the eval exercises the structural scorer, not the adapter"
        ),
        "",
        "## Limits",
        "",
        "- This is a deterministic structural prefilter on a small curated corpus;",
        "  it is not Prompt Guard 2 / Llama Guard / PIDS-Bench, and the numbers do",
        "  not transfer to production traffic.",
        "- A hit is evidence about text; TMA-NM origin/taint + the act gate remain",
        "  the authority for consequential actions.",
        "- The threshold is chosen on the benign half of THIS corpus at the target",
        "  FPR; re-calibrate on real own-tool-output before enforcing.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        default=str(Path(__file__).resolve().parents[1] / "tests/fixtures/injection_corpus.jsonl"),
    )
    parser.add_argument("--target-fpr", type=float, default=None)
    parser.add_argument("--output", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from app.services import injection_detector

    target_fpr = args.target_fpr
    if target_fpr is None:
        try:
            from app.core.config import get_config
            target_fpr = get_config().injection_detector_target_fpr
        except Exception:
            target_fpr = 0.01
    try:
        corpus = injection_detector.load_corpus(args.corpus)
    except OSError as exc:
        print(f"corpus unavailable: {exc}", file=sys.stderr)
        return 1
    report = injection_detector.evaluate_corpus(
        corpus, target_fpr=target_fpr,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    text = build_report(report, args.corpus)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"report written: {args.output}")
    else:
        print(text)
    return 0 if report["fpr"] <= float(target_fpr) else 2


if __name__ == "__main__":
    raise SystemExit(main())
