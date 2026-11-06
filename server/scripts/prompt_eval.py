#!/usr/bin/env python3
"""P2 golden-set harness v0 (deterministic; no provider calls).

Usage (from backend/):
  python -m scripts.prompt_eval
  python -m scripts.prompt_eval --check
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

CASES = Path(__file__).resolve().parents[1] / "tests" / "prompt_evals" / "cases.json"
REGRESSIONS = Path(__file__).resolve().parents[1] / "tests" / "prompt_evals" / "regressions.json"


def _builder_prompt() -> str:
    import asyncio
    from types import SimpleNamespace
    from app.prompts.system_agent import build_agent_system_prompt

    return asyncio.run(build_agent_system_prompt(
        assistant=SimpleNamespace(system_prompt=""),
        shared_context=SimpleNamespace(
            memory_summary="", dream_summary="", memory_entries=[]),
        workspace=SimpleNamespace(root_path="/tmp/prompt-eval"),
        user=SimpleNamespace(id="eval"),
        identity_context="",
        conversation_id="",
        deathmatch_mode=False,
        offered_tool_names=["web_search"],
    ))


def run() -> tuple[list[str], int]:
    from app.prompts.compiler import compile_prompt, lint_text
    from app.prompts.contracts import contract_for
    from app.prompts.registry import require

    cases = json.loads(CASES.read_text(encoding="utf-8"))
    failures: list[str] = []
    for case in cases:
        cid = case.get("id", "?")
        if case.get("builder") == "agent_system":
            text = _builder_prompt()
            for needle in case.get("must_contain") or []:
                if needle not in text:
                    failures.append(f"{cid}: missing {needle!r}")
            for needle in case.get("must_not_contain") or []:
                if needle in text:
                    failures.append(f"{cid}: forbidden {needle!r}")
            continue
        try:
            spec = require(case["prompt_id"])
        except KeyError as exc:
            failures.append(f"{cid}: {exc}")
            continue
        if case.get("version") and spec.version != case["version"]:
            failures.append(f"{cid}: version {spec.version} != {case['version']}")
        if case.get("owner") and spec.owner != case["owner"]:
            failures.append(f"{cid}: owner {spec.owner} != {case['owner']}")
        if case.get("output_schema"):
            if spec.output_schema != case["output_schema"]:
                failures.append(
                    f"{cid}: output_schema {spec.output_schema} != {case['output_schema']}")
            elif contract_for(spec.output_schema) is None:
                failures.append(f"{cid}: no contract registered for {spec.output_schema}")
        compiled = compile_prompt(spec)
        for needle in case.get("must_contain") or []:
            if needle not in compiled.text:
                failures.append(f"{cid}: missing {needle!r}")
        for needle in case.get("must_not_contain") or []:
            if needle in compiled.text:
                failures.append(f"{cid}: forbidden {needle!r}")
        if case.get("lint_clean"):
            issues = lint_text(compiled.text)
            if issues:
                failures.append(f"{cid}: lint {issues}")
    # Production-feedback regressions are executable cases too (P2.5 loop).
    # C-10 intake rows are review-pending: they carry the failure content and
    # label, but no prompt-text assertion yet, so they must NOT inflate the
    # executable pass count (A4.9 review).
    try:
        regressions = json.loads(REGRESSIONS.read_text(encoding="utf-8"))
    except Exception:
        regressions = []
    pending = [c for c in regressions if c.get("kind") == "intake"]
    executable = [c for c in regressions if c.get("kind") != "intake"]
    for case in executable:
        cid = case.get("id", case.get("key", "?"))
        try:
            spec = require(case["prompt_id"])
        except KeyError as exc:
            failures.append(f"{cid}: {exc}")
            continue
        compiled = compile_prompt(spec)
        for needle in case.get("must_contain") or []:
            if needle not in compiled.text:
                failures.append(f"{cid}: missing {needle!r}")
        for needle in case.get("must_not_contain") or []:
            if needle in compiled.text:
                failures.append(f"{cid}: forbidden {needle!r}")
    return failures, len(cases) + len(executable)


def pending_intake_count() -> int:
    try:
        rows = json.loads(REGRESSIONS.read_text(encoding="utf-8"))
    except Exception:
        return 0
    return sum(1 for row in rows if isinstance(row, dict)
               and row.get("kind") == "intake")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    failures, total = run()
    pending = pending_intake_count()
    suffix = f" (+{pending} intake pending review)" if pending else ""
    if failures:
        print("\n".join(failures))
        print(f"prompt_eval: {len(failures)} failure(s) / {total} cases{suffix}")
        return 1 if args.check else 0
    print(f"prompt_eval: {total}/{total} passed{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
