"""Offline P4 evaluation: planted-fact recall for the 2D window + compaction.

Deterministic (no LLM).  A synthetic session plants distinctive facts at the
head, middle, and tail, and in fields that only the full-field estimator sees
(tool results, reasoning).  It then reports, per mode:

* planted-fact recall in the retained window;
* the **real** full-field token size of that window (legacy counts content
  only, so its window can silently exceed the budget);
* the same recall after a fair post-hoc trim to the real budget (what a
  provider limit would force anyway).

Compaction recall is reported before/after ``repair_summary``.

Usage (from backend/):
    python -m scripts.eval_context_window --output ../tests/context_window_eval_report.md
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

FACTS = {
    "head_content": "PLANT-HEAD-7741",
    "mid_content": "PLANT-MID-3320",
    "tail_content": "PLANT-TAIL-9081",
    "tool_only": "artifacts/report_7741.csv",
    "reasoning_only": "BUDGET-48213",
    "constraint": "不得删除 audit.log",
}


def _session(n: int = 120) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for i in range(n):
        content = f"turn {i}: discuss the retention plan and the current task state."
        if i == 3:
            content += f" Remember the value {FACTS['head_content']} from the first request."
        if i == 60:
            content += f" The middle checkpoint is {FACTS['mid_content']}."
        if i == 118:
            content += f" Final answer must mention {FACTS['tail_content']}."
        msg: dict[str, Any] = {"role": "user" if i % 2 else "assistant", "content": content}
        if i % 4 == 0:
            msg["tool_results"] = json.dumps({
                "ok": True,
                "stdout": ("log line " * 700),
            })
        if i == 30:
            msg["tool_results"] = json.dumps({
                "path": FACTS["tool_only"], "rows": 12,
                "stdout": ("log line " * 300),
            })
        if i % 5 == 0:
            msg["reasoning_content"] = "consider the budget " * 60
        if i == 110:
            msg["reasoning_content"] = f"internal budget id {FACTS['reasoning_only']}"
        if i == 95:
            msg["content"] = str(msg.get("content") or "") + f" Constraint: {FACTS['constraint']}."
        messages.append(msg)
    return messages


def _recall(window: list[dict[str, Any]]) -> dict[str, bool]:
    blob = json.dumps(window, ensure_ascii=False)
    return {name: value in blob for name, value in FACTS.items()}


def _actual_tokens(window: list[dict[str, Any]]) -> int:
    from app.services.history_window_service import estimate_message_tokens
    return sum(estimate_message_tokens(m) for m in window)


def _trim_to_budget(window: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    kept = list(window)
    while kept and _actual_tokens(kept) > budget:
        kept.pop(0)
    return kept


def build_report(*, budget: int = 4000, max_messages: int = 40) -> dict[str, Any]:
    from app.services import history_window_service as hw
    from app.services import compaction_verification as cv

    messages = _session()
    legacy = hw.select_history_window_meta(
        messages, max_messages, budget, mode="legacy",
    )
    window_2d = hw.select_history_window_meta(
        messages, max_messages, budget, mode="2d",
    )
    legacy_trimmed = _trim_to_budget(legacy.messages, budget)
    modes = {
        "legacy": {
            "kept_messages": len(legacy.messages),
            "real_tokens": _actual_tokens(legacy.messages),
            "recall": _recall(legacy.messages),
            "recall_after_fair_trim": _recall(legacy_trimmed),
            "real_tokens_after_fair_trim": _actual_tokens(legacy_trimmed),
        },
        "2d": {
            "kept_messages": len(window_2d.messages),
            "real_tokens": _actual_tokens(window_2d.messages),
            "recall": _recall(window_2d.messages),
            "recall_after_fair_trim": _recall(_trim_to_budget(window_2d.messages, budget)),
            "real_tokens_after_fair_trim": _actual_tokens(
                _trim_to_budget(window_2d.messages, budget)
            ),
        },
    }

    # Compaction: a lossy summary that keeps only half the facts.
    middle = messages[:100]
    facts = cv.extract_key_facts(middle, max_facts=20)
    lossy_summary = (
        "The user asked about the retention plan. Keep the head value "
        f"{FACTS['head_content']} and the constraint {FACTS['constraint']}."
    )
    before_recall, before_missing = cv.fact_recall(facts, lossy_summary)
    repaired, stats = cv.repair_summary(middle, lossy_summary, floor=1.0)
    after_recall, after_missing = cv.fact_recall(facts, repaired)
    compaction = {
        "facts": facts,
        "recall_before": before_recall,
        "missing_before": before_missing,
        "recall_after": after_recall,
        "missing_after": after_missing,
        "repaired": stats.get("repaired"),
        "repaired_facts": stats.get("repaired_facts", 0),
        "summary_before_chars": len(lossy_summary),
        "summary_after_chars": len(repaired),
    }
    return {
        "budget": budget,
        "max_messages": max_messages,
        "facts": FACTS,
        "modes": modes,
        "compaction": compaction,
    }


def render(result: dict[str, Any]) -> str:
    lines = [
        "# Context window evaluation (planted facts)",
        "",
        f"- token budget: {result['budget']}",
        f"- max messages: {result['max_messages']}",
        "",
        "## Window recall",
        "",
        "| mode | kept | real tokens | recalls (head/mid/tail/tool/reasoning/constraint) | recall after fair trim |",
        "|---|---|---|---|---|",
    ]
    order = ("head_content", "mid_content", "tail_content", "tool_only", "reasoning_only", "constraint")
    for mode, row in result["modes"].items():
        marks = "/".join("1" if row["recall"][k] else "0" for k in order)
        trimmed = "/".join("1" if row["recall_after_fair_trim"][k] else "0" for k in order)
        lines.append(
            f"| {mode} | {row['kept_messages']} | {row['real_tokens']} | {marks} | {trimmed} |"
        )
    compaction = result["compaction"]
    lines += [
        "",
        "## Compaction verification",
        "",
        f"- facts extracted: {len(compaction['facts'])}",
        f"- recall before repair: {compaction['recall_before']:.2%}",
        f"- recall after repair: {compaction['recall_after']:.2%}",
        f"- repaired: {compaction['repaired']} ({compaction['repaired_facts']} verbatim facts)",
        f"- summary size: {compaction['summary_before_chars']} -> {compaction['summary_after_chars']} chars",
        "",
        "## Limits",
        "",
        "- Synthetic session; it measures deterministic field coverage, not model attention.",
        "- `real tokens` is the full-field estimate of the retained window; legacy can",
        "  exceed the configured budget because it counts content only.",
        "- Raw recall is not comparable across modes at different real token sizes;",
        "  the 'after fair trim' column trims each window to the same real budget.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget", type=int, default=4000)
    parser.add_argument("--max-messages", type=int, default=40)
    parser.add_argument("--output", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = build_report(budget=args.budget, max_messages=args.max_messages)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    text = render(result)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"report written: {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
