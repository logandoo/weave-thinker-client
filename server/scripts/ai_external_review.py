#!/usr/bin/env python3
"""External review / fresh-context judgement via the project model gateway.

The OpenCode reviewer could not use its own provider key (expired), so this
script uses the project's configured MIMO endpoint (key lives in the
gitignored ``config_model.toml``) and the requested ``mimo-v2.6-flash`` model.

Usage (from backend/):
  python -m scripts.ai_external_review --mode review \
      --brief /tmp/review_brief.md --diff /tmp/wave.diff --out tests/review_body.md
  python -m scripts.ai_external_review --mode fcv \
      --brief tests/fcv_brief.md --evidence /tmp/fcv_evidence.md \
      --out tests/fcv_report.md
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, ".")

_REVIEW_SYSTEM = """你是独立的对抗性代码审查员。只依据提供的 brief 与 diff 工作，
不假设作者的解释正确。重点找：行为语义变化、控制流/异常吞没、变量遮蔽、
MRO/导入循环、并发/对象身份、测试弱化。输出格式：
Verdict: ready | ready-with-fixes | not-ready
Strengths: ...
Findings: 每条 `[Critical|Important|Minor] [Bugs|Security|Compliance] file:line — why`
Assessment: 2-4 句。不要修改任何文件。"""

_FCV_SYSTEM = """你是 fresh-context 验收员，只依据提供的验收标准与已执行命令输出
判断，不猜测未给出的内容。对每个 criterion 给 PASS/FAIL 并引用输出证据；
任何 claimed/observed 不一致都要列出。最后输出 `Overall: pass|fail` 与
`Mismatches:`。不要修改任何文件。"""


async def _call(args) -> str:
    from app.model_gateway import factory
    from app.model_gateway.registry import get_model_registry
    from app.prompts.observability import redact_secrets

    endpoint = get_model_registry().get(args.alias)
    if args.model:
        endpoint = replace(endpoint, model_name=args.model)
    llm = factory.build_llm_service(endpoint)
    brief = Path(args.brief).read_text(encoding="utf-8")
    payload = brief
    if args.mode == "review":
        payload += "\n\n## DIFF\n" + Path(args.diff).read_text(encoding="utf-8")
        system = _REVIEW_SYSTEM
    else:
        payload += "\n\n## EXECUTED EVIDENCE\n" + Path(args.evidence).read_text(encoding="utf-8")
        system = _FCV_SYSTEM
    try:
        out = await llm.complete_chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": payload}],
            temperature=0.0,
            max_tokens=args.max_tokens,
        )
    except Exception as exc:
        raise SystemExit(f"model call failed: {type(exc).__name__}: {redact_secrets(str(exc))}")
    return str(out or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("review", "fcv"), required=True)
    ap.add_argument("--brief", required=True)
    ap.add_argument("--diff", default="")
    ap.add_argument("--evidence", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--alias", default="mimo")
    ap.add_argument("--model", default="mimo-v2.6-flash")
    ap.add_argument("--max-tokens", type=int, default=8000)
    args = ap.parse_args()
    text = asyncio.run(_call(args))
    if not text:
        raise SystemExit("model returned empty output")
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({len(text)} chars) via {args.alias}/{args.model}")
    print(text[:300].replace("\n", " "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
