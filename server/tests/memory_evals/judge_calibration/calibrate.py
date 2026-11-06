#!/usr/bin/env python3
"""W0-3 judge 校准台：judge ↔ gold 一致率（人工标注校准集；零 DB/零 LLM/零网络）。

用法（cd backend）:
    python tests/memory_evals/judge_calibration/calibrate.py
    python tests/memory_evals/judge_calibration/calibrate.py \
        --cases tests/memory_evals/judge_calibration/cases.sample.jsonl \
        --min-agreement 0.8

口径（详见同目录 schema.md）：
- 仅 `human_verified=true` 的行计入一致率；`human_verified=false` 的行与空集
  **排除在一致率外**并显式报告计数；
- 维度 = 计入行 `gold_labels` 键并集；judge 缺维度记不一致；judge 多出的维度忽略；
- `overall_agreement` = Σ(行,维度) 匹配数 / Σ(行,维度) 对数（micro）；
- `overall_agreement < --min-agreement`（默认 0.8）或无可计入行 →
  输出「judge 不得进验收」结论并 exit 2（验收链路不得采信 judge）；
- 数据错误（JSON 非法/缺字段）→ 报错 exit 1（不与阈值结论混淆）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

DEFAULT_CASES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.jsonl")
REQUIRED_KEYS = ("id", "query", "gold_answer", "judge_answer",
                 "gold_labels", "judge_labels", "human_verified")

NOT_ADMITTED = "judge 不得进验收"
ADMITTED = "judge 可进验收"


# ── 装载/校验（纯函数） ───────────────────────────────────────────────────

def load_cases(path: str) -> List[Dict[str, Any]]:
    """读取 cases.jsonl；文件不存在 → 空列表（=无 cases，由 main 报告）。

    JSON 非法或缺 REQUIRED_KEYS → ValueError（数据错误 exit 1）。
    """
    if not os.path.exists(path):
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON ({exc})")
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{lineno}: entry must be a JSON object")
            for key in REQUIRED_KEYS:
                if key not in row:
                    raise ValueError(f"{path}:{lineno}: missing key: {key}")
            if not isinstance(row["gold_labels"], dict) or not isinstance(row["judge_labels"], dict):
                raise ValueError(f"{path}:{lineno}: gold_labels/judge_labels must be objects")
            rows.append(row)
    return rows


# ── 一致率（纯函数） ──────────────────────────────────────────────────────

def compute_agreement(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """judge↔gold 一致率（overall + 分维度）；human_verified=false 排除并计数。"""
    included = [r for r in rows if r.get("human_verified") is True]
    excluded_unverified = len(rows) - len(included)
    dims = sorted({k for r in included for k in r["gold_labels"]})
    per_dimension: Dict[str, Dict[str, Any]] = {}
    total_pairs = 0
    total_matches = 0
    exact_rows = 0
    for dim in dims:
        gold_rows = [r for r in included if dim in r["gold_labels"]]
        matches = sum(1 for r in gold_rows
                      if r["judge_labels"].get(dim) == r["gold_labels"][dim])
        missing = sum(1 for r in gold_rows if dim not in r["judge_labels"])
        per_dimension[dim] = {
            "agreement": (round(matches / len(gold_rows), 4) if gold_rows else None),
            "matches": matches,
            "rows": len(gold_rows),
            "judge_missing": missing,
        }
        total_pairs += len(gold_rows)
        total_matches += matches
    for r in included:
        if all(r["judge_labels"].get(d) == v for d, v in r["gold_labels"].items()):
            exact_rows += 1
    return {
        "cases_total": len(rows),
        "included": len(included),
        "excluded_human_verified_false": excluded_unverified,
        "dimensions": dims,
        "per_dimension": per_dimension,
        "pairs": total_pairs,
        "matches": total_matches,
        "overall_agreement": (round(total_matches / total_pairs, 4)
                              if total_pairs else None),
        "exact_match_rate": (round(exact_rows / len(included), 4) if included else None),
    }


def render_report(report: Dict[str, Any], min_agreement: float) -> str:
    """人类可读报告 + 验收结论（阈值判定的唯一出口文本）。"""
    lines = [
        "judge 校准报告（judge ↔ gold 一致率）",
        f"cases_total={report['cases_total']} included={report['included']} "
        f"excluded_human_verified_false={report['excluded_human_verified_false']}",
    ]
    if report["cases_total"] == 0:
        lines.append("no cases: 校准集为空（50 例人工标注未完成）")
    if report["included"] == 0:
        lines.append("per-dimension: （无计入行，一致率不可得）")
    else:
        for dim in report["dimensions"]:
            d = report["per_dimension"][dim]
            lines.append(f"per-dimension {dim}: {d['agreement']:.3f} "
                         f"({d['matches']}/{d['rows']}, judge_missing={d['judge_missing']})")
    overall = report["overall_agreement"]
    if overall is None:
        lines.append("overall_agreement: n/a (0 pairs)")
    else:
        lines.append(f"overall_agreement: {overall:.3f} "
                     f"({report['matches']}/{report['pairs']} pairs) "
                     f"exact_match_rate={report['exact_match_rate']:.3f}")
    threshold = f"阈值 {min_agreement:.3f}"
    if overall is None or overall < min_agreement:
        reason = ("无可计入行" if overall is None
                  else f"overall {overall:.3f} < {threshold}")
        lines.append(f"{NOT_ADMITTED}（{reason}；judge 只能作参考信号，不得进验收）")
    else:
        lines.append(f"{ADMITTED}（overall {overall:.3f} ≥ {threshold}）")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="memory_evals.judge_calibration.calibrate",
        description="W0-3 judge 校准台：judge↔gold 一致率（不连库/不调 LLM）")
    parser.add_argument("--cases", default=DEFAULT_CASES, help="校准集 JSONL 路径")
    parser.add_argument("--min-agreement", type=float, default=0.8,
                        help="judge 进验收的一致率阈值（默认 0.8）")
    args = parser.parse_args(argv)

    try:
        rows = load_cases(args.cases)
    except ValueError as exc:
        print(f"calibrate error: {exc}", file=sys.stderr)
        return 1
    report = compute_agreement(rows)
    print(render_report(report, args.min_agreement))
    overall = report["overall_agreement"]
    if overall is None or overall < args.min_agreement:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
