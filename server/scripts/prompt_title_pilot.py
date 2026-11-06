#!/usr/bin/env python3
"""P4.3 title-prompt optimization pilot (plan 2026-10-18).

A small, dependency-free GEPA-style variant search over the title prompt:
3 candidates × 10 cases against a real model, scored by structural title
contract.  A candidate is retained only if it beats the baseline; otherwise
the baseline stays.  This is the "10–30 examples to start" pilot scope, not a
claim that DSPy is unnecessary in general.

Usage (from backend/):
  python -m scripts.prompt_title_pilot --alias deepseek
"""
from __future__ import annotations

import argparse
import asyncio
import re
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, ".")

OUT = Path(__file__).resolve().parents[1] / "tests" / "prompt_evals" / "title_pilot_live.json"

_CASES = [
    ("Python 的 GIL 是什么？", ["GIL", "解释器", "Python"]),
    ("怎么在 Linux 上安装 Docker？", ["Docker", "安装", "Linux"]),
    ("总结一下远程办公的优缺点", ["远程办公", "优缺点", "总结"]),
    ("解释 RAG 和微调的区别", ["RAG", "微调", "区别"]),
    ("帮我写一封请假邮件", ["请假", "邮件"]),
    ("2026 年 AI 行业有哪些趋势？", ["AI", "趋势", "2026"]),
    ("如何用 pandas 做数据清洗", ["pandas", "数据清洗"]),
    ("推荐几本系统设计的书", ["系统设计", "书", "推荐"]),
    ("什么是向量数据库", ["向量数据库", "定义"]),
    ("分析一下这个季度的销售数据", ["销售", "季度", "分析"]),
    ("如何在 Excel 中做透视表", ["Excel", "透视表"]),
    ("解释 Transformer 注意力机制", ["Transformer", "注意力"]),
    ("写一个 Python 快速排序", ["Python", "排序"]),
    ("总结 SQL 注入的防护", ["SQL", "注入", "防护"]),
    ("2026 年新能源汽车趋势", ["新能源", "趋势"]),
    ("推荐几部科幻电影", ["科幻", "电影", "推荐"]),
    ("什么是零知识证明", ["零知识证明"]),
    ("分析一下这份财报", ["财报", "分析"]),
    ("如何学习 Rust", ["Rust", "学习"]),
    ("写一首关于秋天的诗", ["秋天", "诗"]),
]

_CANDIDATES = {
    "baseline": None,  # filled from app.prompts.tools
    "strict": (
        "你是中文对话标题生成器。只输出一个 JSON 对象：{\"title\": \"...\"}。\n"
        "标题必须是 6-16 个汉字的名词短语，不含问号、书名号或标点，"
        "不照抄用户问题，不包含“如何/什么/为什么/怎么/请问”等问句成分。"
        "不要解释、不要 Markdown、不要代码块。"
    ),
    "compact": (
        "生成一个 4-12 字的中文名词短语标题。只输出 {\"title\":\"...\"}。"
        "禁止问句、标点、解释与用户原句照抄。"
    ),
}


def _score(query: str, raw: str, must: list[str] | None = None) -> Dict[str, Any]:
    from app.prompts.contracts import TitleResult, parse_contract

    value, failure = parse_contract(TitleResult, raw)
    if failure is not None or value is None:
        return {"ok": False, "reason": f"json:{failure.kind}"}
    title = str(value.title or "").strip()
    if not title:
        return {"ok": False, "reason": "empty"}
    if len(title) < 2 or len(title) > 20:
        return {"ok": False, "reason": f"length:{len(title)}"}
    if any(ch in title for ch in "?？!！"):
        return {"ok": False, "reason": "question mark"}
    if any(w in title for w in ("如何", "什么", "为什么", "怎么", "请问")):
        return {"ok": False, "reason": "question word"}
    if title == query.strip():
        return {"ok": False, "reason": "copied query"}
    if not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", title):
        return {"ok": False, "reason": "no meaningful text"}
    if must and not any(str(k).lower() in title.lower() for k in must):
        return {"ok": False, "reason": "missing keyword"}
    return {"ok": True, "reason": "ok", "title": title}


async def _run(alias: str, timeout: float) -> Dict[str, Any]:
    from app.model_gateway import factory
    from app.model_gateway.registry import get_model_registry
    from app.prompts.observability import redact_secrets
    from app.prompts.tools import _TITLE_SYSTEM_PROMPT

    prompts = dict(_CANDIDATES)
    prompts["baseline"] = _TITLE_SYSTEM_PROMPT
    llm = factory.build_llm_service(get_model_registry().get(alias))
    results: Dict[str, Any] = {}
    for name, system in prompts.items():
        rows = []
        for query, must in _CASES:
            started = time.perf_counter()
            raw, error = "", ""
            try:
                raw = await asyncio.wait_for(
                    llm.complete_chat(
                        [{"role": "system", "content": system},
                         {"role": "user", "content": f"用户问题：{query}"}],
                        temperature=0.0,
                        max_tokens=400,
                    ),
                    timeout=timeout,
                )
            except Exception as exc:
                error = redact_secrets(f"{type(exc).__name__}: {exc}")
            scored = _score(query, raw, must) if not error else {"ok": False, "reason": error}
            if scored.get("title"):
                scored["title"] = redact_secrets(str(scored["title"]))
            rows.append({
                "query": query,
                **scored,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "output": redact_secrets(str(raw or "")[:200]),
            })
        results[name] = {
            "score": sum(1 for r in rows if r.get("ok")),
            "cases": len(rows),
            "rows": rows,
        }
    baseline = results["baseline"]["score"]
    retained = "baseline"
    for name, data in results.items():
        if name != "baseline" and data["score"] > results[retained]["score"]:
            retained = name
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": f"live-pilot:{alias}",
        "live": True,
        "baseline_score": baseline,
        "retained": retained,
        "results": results,
        "note": "candidate retained only if score > baseline; otherwise baseline stays",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", default="deepseek")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    data = asyncio.run(_run(args.alias, args.timeout))
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    print(json.dumps({
        "baseline_score": data["baseline_score"],
        "retained": data["retained"],
        "scores": {k: v["score"] for k, v in data["results"].items()},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
