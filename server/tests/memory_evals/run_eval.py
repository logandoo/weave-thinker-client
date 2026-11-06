#!/usr/bin/env python3
"""M4a 离线记忆评测台 runner（metadata-only；零 DB/零 LLM/零网络；绝不连生产库）。

用法（cd backend）:
    python tests/memory_evals/run_eval.py
    python tests/memory_evals/run_eval.py --categories temporal_list,missing_evidence
    python tests/memory_evals/run_eval.py --arms provider_routing=off,provider_routing=on \
        --out tests/memory_evals/results/latest.json

跨配置矩阵臂（至少 provider_routing on/off 两臂，M1a A/B 判据）；结果 JSON 由
`scripts/memory_eval_report.py` 汇总为 markdown。LLM 判定面缺省跳过并标
UNVERIFIED（--with-llm 预留，未接线）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))
sys.path.insert(0, _HERE)

import harness  # noqa: E402

DEFAULT_ARMS = "provider_routing=off,provider_routing=on"


def parse_arms(spec: str) -> List[Dict[str, Any]]:
    """臂规格解析（纯函数）：`provider_routing=off|on`（裸 off/on 同义）。"""
    arms: List[Dict[str, Any]] = []
    for token in (spec or "").split(","):
        token = token.strip()
        if not token:
            continue
        name = token if "=" in token else f"provider_routing={token}"
        key, _, value = name.partition("=")
        if key != "provider_routing" or value not in ("on", "off"):
            raise ValueError(f"unknown arm spec: {token}")
        arms.append({
            "name": name,
            "provider_routing_enabled": value == "on",
        })
    if not arms:
        raise ValueError("empty arm spec")
    return arms


def run_matrix(scenarios: List[Dict[str, Any]], arms: List[Dict[str, Any]]) -> Dict[str, Any]:
    """跨配置矩阵入口：每场景 × 每臂 → 指标记录 + 汇总（假检索注入，零 LLM）。"""
    runs = [harness.run_scenario(s, arm) for s in scenarios for arm in arms]
    return {
        "meta": {
            "runner": "memory_evals.run_eval",
            "version": 1,
            "llm": "skipped",
            "arms": [a.get("name") for a in arms],
        },
        "scenarios": runs,
        "summary": harness.summarize(runs),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="memory_evals.run_eval",
        description="M4a 离线记忆评测台 runner（metadata-only，不连生产库）")
    parser.add_argument("--scenarios-dir", default=harness.SCENARIOS_DIR)
    parser.add_argument("--ids", default=None, help="逗号分隔场景 id 过滤")
    parser.add_argument("--categories", default=None, help="逗号分隔类别过滤")
    parser.add_argument("--arms", default=DEFAULT_ARMS)
    parser.add_argument("--out", default=os.path.join(_HERE, "results", "latest.json"))
    args = parser.parse_args(argv)

    arms = parse_arms(args.arms)
    scenarios = harness.load_scenarios(args.scenarios_dir)
    if args.ids:
        keep = {x.strip() for x in args.ids.split(",") if x.strip()}
        scenarios = [s for s in scenarios if s["id"] in keep]
    if args.categories:
        keep = {x.strip() for x in args.categories.split(",") if x.strip()}
        scenarios = [s for s in scenarios if s["category"] in keep]
    if not scenarios:
        print("no scenarios selected", file=sys.stderr)
        return 1

    results = run_matrix(scenarios, arms)
    out_path = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    summary = results["summary"]
    print(f"runs={summary['total_runs']} passed={summary['passed']} "
          f"failed={summary['failed']} hit@k={summary['hit_at_k_mean']} "
          f"zero_hit={summary['zero_hit_rate']} "
          f"tokens={summary['mean_injected_tokens']} "
          f"latency_ms={summary['mean_latency_ms']} "
          f"llm_unverified={summary['llm_unverified_count']}")
    print(f"written: {out_path}")
    return 0 if summary["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
