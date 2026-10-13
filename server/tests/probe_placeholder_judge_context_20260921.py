"""M5 行为探针：占位判定 judge 在收到【任务要求上下文】后能否区分
「可执行而未执行（研究语域重包装）」与「客观不可得（如实说明）」。

背景（A4.9 r2 M1/M5）：原 judge 只收交付物摘录，text-only 不可判定
"本应执行 vs 客观不可得"；本波给 judge 注入任务要求上下文
（`_placeholder_judge_obligation_context` + `_build_placeholder_judge_content`）。
真实 LLM（purpose=agent.audit 同源 `judge_json`，task=execution_placeholders，
temperature 默认；每次运行 2 次取一致）。
判定：P1 旧式占位 → true · P1b 研究语域重包装 → true（本波关键）
      · P2 客观不可得如实说明 → false · P3 已完成结果 → false。

Run: cd backend && ../.venv/bin/python tests/probe_placeholder_judge_context_20260921.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.model_gateway.gateway import get_model_gateway  # noqa: E402
from app.services.deathmatch_service import (  # noqa: E402
    _PLACEHOLDER_JUDGE_PROMPT,
    _build_placeholder_judge_content,
    _placeholder_judge_obligation_context,
)

# 任务要求：实证参数要求真实执行（must_run + 命令 + 产出文件）
CONTEXT_REQUIRED = _placeholder_judge_obligation_context(
    goal="完成一篇实证论文：跑出 P1—P6 的基准回归系数、标准误与样本量，并交付 paper.md",
    criteria_texts=["交付物不得以未执行占位（待执行/受限估计/待填报/不填报数值等）代替实际结果"],
    steps=[
        {
            "id": "s5",
            "description": "基准回归：对 P1—P6 逐项估计并报告系数、标准误、样本量",
            "expected_output": "paper.md 第6章含 P1—P6 的系数、标准误与观测数",
            "evidence_spec": {
                "kind": "compute",
                "must_run": True,
                "receipt": {"cmds": ["python analysis/run.py"], "outputs": ["paper.md"], "min_rows": 1},
            },
        }
    ],
    files=["paper.md"],
)

# 任务要求：数据客观不可得（生产形态：ob1 义务文本 + 步骤承诺；允许如实说明）
CONTEXT_INFEASIBLE = _placeholder_judge_obligation_context(
    goal="完成一篇实证论文；省际面板与清欠类数据不公开时如实说明限制",
    criteria_texts=[
        "交付物不得以未执行占位（待执行/受限估计/待填报/不填报数值等）代替实际结果——"
        "无法执行的必须走降级协议（结构化授权 + 显著标注）",
    ],
    steps=[
        {
            "id": "s5",
            "description": "在公开数据可得范围内分析；不可得数据如实说明（已尝试获取）",
            "expected_output": "paper.md 数据与识别边界说明（含已尝试获取的记录）",
            "output_files": ["paper.md"],
        }
    ],
    files=["paper.md"],
)

DOC_OLD_LABEL = (
    "### 6.3 基准回归：设定、识别假设与执行状态\n"
    "表6-2 基准回归设定与执行状态\n"
    "| 命题 | ... | 执行状态 |\n| P1 | ... | 受限估计：不填报数值 |\n"
    "表注：系数、标准误、显著性标记与观测数在本稿中一律不填报，待数据闭环后按表列设计执行。"
)
DOC_RESEARCH_REWRITE = (
    "### 6.3 基准回归\n"
    "由于数据与识别约束，本文对 P1—P6 的相关参数未估计、未报告系数与标准误（识别边界）。"
    "公开序列上的描述统计与相关分析尚未纳入本稿。"
)
DOC_HONEST_LIMIT = (
    "### 6.3 数据与识别边界\n"
    "省际面板与清欠类数据未公开，经尝试获取（公开渠道检索与申请）均不可得，"
    "相关参数无法估计；本文如实说明该数据缺口与识别边界，未报告系数与标准误。"
)
DOC_COMPLETED = (
    "### 6.3 基准回归结果\n"
    "P1 传导阻滞：货币乘数系数 -0.42（标准误 0.11，p<0.01，n=96）；"
    "P2 配置偏向：风险偏好系数 0.17（标准误 0.06，p<0.01，n=384）；"
    "P3 付息消耗：付息负担占比系数 0.31（标准误 0.09，p<0.01，n=248）；"
    "P4 富集与账期：上游利润份额系数 0.22（标准误 0.08，p<0.01，n=131）；"
    "P5 预防性储蓄：存款定期化率系数 0.14（标准误 0.05，p<0.01，n=96）；"
    "P6 就业弹性：工业增加值增速系数 0.28（标准误 0.10，p<0.01，n=240）。"
    "估计命令：python analysis/run.py；结果文件：analysis/results.json（含全部 6 项系数与样本量）。"
)
# A4.9 r3 F1：部分完成（仅覆盖部分承诺条目，无占位措辞）——保守阻断（true）
DOC_PARTIAL = (
    "### 6.3 基准回归\n"
    "本节报告 P1 与 P2 的估计结果：P1 货币乘数系数 -0.42（标准误 0.11，p<0.01，n=96）；"
    "P2 风险偏好系数 0.17（标准误 0.06，p<0.01，n=384）。"
    "估计命令：python analysis/run.py。P3—P6 的估计结果未包含在本节。"
)


async def judge(judge_json, excerpt: str, context: str) -> dict:
    content = _build_placeholder_judge_content(excerpt, context)
    raw = await judge_json(
        _PLACEHOLDER_JUDGE_PROMPT,
        content,
        task="execution_placeholders",
        default=None,
        timeout=120.0,
    )
    if not isinstance(raw, dict):
        return {"ok": None, "evidence": [], "error": "judge_unavailable"}
    return {
        "ok": bool(raw.get("has_unexecuted_placeholders")),
        "evidence": [str(x)[:120] for x in (raw.get("evidence") or [])][:3],
    }


async def main() -> int:
    from app.services.agentic_judge import judge_json

    cases = (
        ("P0_old_label_no_context", DOC_OLD_LABEL, "", True),
        ("P1_old_label_required", DOC_OLD_LABEL, CONTEXT_REQUIRED, True),
        ("P1b_research_rewrite_required", DOC_RESEARCH_REWRITE, CONTEXT_REQUIRED, True),
        ("P2_honest_limit_infeasible", DOC_HONEST_LIMIT, CONTEXT_INFEASIBLE, False),
        ("P3_completed_required", DOC_COMPLETED, CONTEXT_REQUIRED, False),
        ("P4_partial_required", DOC_PARTIAL, CONTEXT_REQUIRED, True),
    )
    results = {}
    all_ok = True
    for name, doc, ctx, expected in cases:
        runs = [await judge(judge_json, doc, ctx) for _ in range(2)]
        got = [r["ok"] for r in runs]
        ok = all(g is expected for g in got)
        all_ok = all_ok and ok
        results[name] = {"expected": expected, "got": got, "runs": runs}
        print("=" * 72)
        print(f"{name}: expected={expected} got={got} {'PASS' if ok else 'FAIL'}")
        for r in runs:
            print(f"  evidence={r['evidence']}")
    print("=" * 72)
    print(f"VERDICT: {json.dumps({k: v['got'] for k, v in results.items()}, ensure_ascii=False)}")
    print(f"PROBE {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
