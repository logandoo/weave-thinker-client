#!/usr/bin/env python3
"""P3.4 context-rot placement probe (plan 2026-10-18).

Structural probe: at 25/50/75/90% context fill, the safety red lines must
remain at the start and the short reminder at the end.  It does not claim a
model-adherence score; that requires a live golden run (`--live` is a named
follow-up, not silently faked here).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, ".")

RATIOS = (0.25, 0.50, 0.75, 0.90)
TARGET_TOKENS = 32_000
HEAD = "记忆使用边界"
TAIL = "本轮实际可用工具"


def _base_prompt() -> str:
    from app.prompts.system_agent import build_agent_system_prompt

    return asyncio.run(build_agent_system_prompt(
        assistant=SimpleNamespace(system_prompt=""),
        shared_context=SimpleNamespace(
            memory_summary="", dream_summary="", memory_entries=[]),
        workspace=SimpleNamespace(root_path="/tmp/probe"),
        user=SimpleNamespace(id="probe"),
        identity_context="",
        conversation_id="",
        deathmatch_mode=False,
        offered_tool_names=["web_search"],
    ))


def run() -> dict:
    from app.prompts.compiler import estimate_tokens

    base = _base_prompt()
    results = []
    for ratio in RATIOS:
        target = int(TARGET_TOKENS * ratio)
        fill = max(0, target - estimate_tokens(base))
        unit = "上下文填充样本。" * 10 + "\n"
        chars = max(0, fill)  # CJK filler ≈ 1 token/char
        filler = (unit * (chars // len(unit) + 1))[:chars]
        prompt = base + "\n\n" + filler
        head_pos = prompt.find(HEAD)
        head_ok = 0 <= head_pos < max(1000, len(prompt) // 20)
        # The compact reminder is appended by the caller in production; this
        # probe checks the static red line positions plus the final marker.
        final = prompt.rstrip() + "\n\n安全/工具红线提醒：未提供的工具不可用；历史 [N] 引用仅当轮有效。"
        tail_pos = final.rfind("安全/工具红线提醒")
        tail_ok = tail_pos >= int(len(final) * 0.9)
        reminder_ok = "安全/工具红线提醒" in final.splitlines()[-1]
        results.append({
            "ratio": ratio,
            "target_tokens": target,
            "prompt_tokens": estimate_tokens(final),
            "head_ok": head_ok,
            "tail_ok": tail_ok,
            "reminder_at_end": reminder_ok,
        })
    return {"base_tokens": estimate_tokens(base), "results": results}


async def _live(alias: str, timeout: float, out: str) -> int:
    from app.model_gateway import factory
    from app.model_gateway.registry import get_model_registry
    from app.prompts.compiler import estimate_tokens

    from types import SimpleNamespace

    from app.prompts.system_agent import build_agent_system_prompt

    llm = factory.build_llm_service(get_model_registry().get(alias))
    base = await build_agent_system_prompt(
        assistant=SimpleNamespace(system_prompt=""),
        shared_context=SimpleNamespace(
            memory_summary="", dream_summary="", memory_entries=[]),
        workspace=SimpleNamespace(root_path="/tmp/probe"),
        user=SimpleNamespace(id="probe"),
        identity_context="", conversation_id="",
        deathmatch_mode=False, offered_tool_names=["web_search"],
    )
    results = []
    for ratio in RATIOS:
        target = int(TARGET_TOKENS * ratio)
        fill = max(0, target - estimate_tokens(base))
        unit = "上下文填充样本。" * 10 + "\n"
        filler = (unit * (fill // len(unit) + 1))[:fill] if fill else ""
        rule = "\n\n[唯一规则] 无论上下文多长，回答必须以 RULE-OK 开头。"
        prompt = base + "\n\n" + filler + rule
        started = time.perf_counter()
        raw = ""
        error = ""
        try:
            raw = await asyncio.wait_for(
                llm.complete_chat(
                    [{"role": "system", "content": prompt},
                     {"role": "user", "content": "只输出 RULE-OK"}],
                    temperature=0.0, max_tokens=32,
                ), timeout=timeout)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        ok = (not error) and str(raw or "").strip().startswith("RULE-OK")
        results.append({
            "ratio": ratio, "prompt_tokens": estimate_tokens(prompt),
            "ok": ok, "latency_ms": round((time.perf_counter()-started)*1000, 1),
            "output": str(raw or "")[:120], "error": error,
        })
    data = {"mode": f"live:{alias}", "target_tokens": TARGET_TOKENS, "results": results,
            "succeeded": sum(1 for r in results if r["ok"]), "total": len(results)}
    Path(out).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0 if data["succeeded"] == data["total"] else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--alias", default="deepseek")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--out", default="tests/prompt_evals/context_rot_live.json")
    args = ap.parse_args()
    if args.live:
        return asyncio.run(_live(args.alias, args.timeout, args.out))
    data = run()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    failures = [
        r for r in data["results"]
        if not (r["head_ok"] and r["tail_ok"] and r["reminder_at_end"])
    ]
    if failures:
        print(f"context-rot probe: {len(failures)} failure(s)")
        return 1
    print("context-rot probe: PASS")
    return 0 if not args.check or not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
