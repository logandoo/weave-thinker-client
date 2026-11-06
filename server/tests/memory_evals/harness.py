"""M4a 离线记忆评测台核心（metadata-only；假检索结果注入；绝不连生产库）。

技术规格源：docs/AUDIT_2026-10-13_deathmatch_memory_papers.md §3.4（M4a）+
§10.4 E4 租户隔离（独立测试用户/独立库/离线行源，生产只读）。沿
`tests/deathmatch_evals/run_eval.py` 的 metadata-only 风格（本仓无该目录时
= 纯 python 脚本 + 内存种子，零 DB/零 LLM/零网络）。

执行面：场景 JSON（`scenarios/`）→ 假检索候选注入 → 注入组装（复用
memory_retrieval_service 的预算/缺证据 marker/路由纯函数）→ 指标：
hit@k · 零命中率 · 注入 token（字符≈token，与 _apply_token_budget 同口径）·
延迟（毫秒）· 采纳率/存储行数（可选）。LLM 判定面缺省跳过并标 UNVERIFIED。
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import app.services.memory_retrieval_service as mrs  # noqa: E402

SCENARIOS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")

CATEGORIES = (
    "cross_session_recall",
    "preference_follow",
    "temporal_list",
    "contradiction_update",
    "missing_evidence",
    # W0-2 扩充（B6/B7/B1·B2/B10/B17）：只追加，前 5 类语义不变
    "stale_state",
    "forget",
    "poisoning",
    "procedural",
    "cjk",
)

REQUIRED_KEYS = ("id", "category", "query", "fake_retrieval", "expect")

DEFAULT_ARM: Dict[str, Any] = {"name": "baseline", "provider_routing_enabled": False}


# ── 场景装载/校验 ─────────────────────────────────────────────────────────

def validate_scenario(scenario: Dict[str, Any]) -> None:
    for key in REQUIRED_KEYS:
        if key not in scenario:
            raise ValueError(f"scenario missing key: {key}")
    if scenario["category"] not in CATEGORIES:
        raise ValueError(f"unknown category: {scenario['category']}")
    if not isinstance(scenario.get("fake_retrieval", {}).get("candidates"), list):
        raise ValueError("fake_retrieval.candidates must be a list")


def load_scenario(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        scenario = json.load(f)
    validate_scenario(scenario)
    return scenario


def load_scenarios(directory: Optional[str] = None) -> List[Dict[str, Any]]:
    directory = directory or SCENARIOS_DIR
    out: List[Dict[str, Any]] = []
    for name in sorted(os.listdir(directory)):
        if name.endswith(".json"):
            out.append(load_scenario(os.path.join(directory, name)))
    return out


# ── 假检索 + 注入组装（metadata-only，零 DB/零 LLM） ───────────────────────

def build_stage0(scenario: Dict[str, Any]) -> Any:
    st = scenario.get("stage0") or {}
    return mrs.Stage0Result(
        keywords=list(st.get("keywords") or []),
        time_range=None,
        query_type=str(st.get("query_type") or "semantic"),
        include_expired=bool(st.get("include_expired", False)),
    )


def fake_retrieve(scenario: Dict[str, Any]) -> List[Any]:
    """指标层假检索结果注入（场景自带候选，不触真实检索/DB）。"""
    out = []
    for c in (scenario.get("fake_retrieval") or {}).get("candidates") or []:
        out.append(mrs.RetrievalCandidate(
            id=str(c.get("id")), tier=str(c.get("tier") or "concept"),
            content=str(c.get("content") or ""),
            score=float(c.get("score") or 0.0),
            metadata=dict(c.get("metadata") or {}),
        ))
    return out


def select_injected(candidates: List[Any]) -> List[Any]:
    """注入集选择（仿真实 _select_injected 的元数据面）：矛盾更新过时单元
    （superseded/valid_to）不注入；按分数降序、同分按 id 稳定序。"""
    ok = []
    for c in candidates:
        md = c.metadata or {}
        if md.get("superseded") or md.get("valid_to"):
            continue
        ok.append(c)
    ok.sort(key=lambda c: (-float(c.score), str(c.id)))
    return ok


def assemble_injection(scenario: Dict[str, Any], injected: List[Any]) -> str:
    """确定性注入组装（段头与真实 _build_injection_context_ex 对齐）+
    M2a 缺证据 marker（真实纯函数，尾部不计预算）。"""
    query = str(scenario.get("query") or "")
    blocks: List[str] = []
    concepts = ((scenario.get("seed") or {}).get("concepts")) or []
    if concepts:
        lines = ["[用户长期记忆总览]"]
        for c in concepts[:3]:
            name = str(c.get("canonical_name") or "")
            content = str(c.get("content") or "")[:40]
            lines.append(f"- {name}: {content}".rstrip())
        blocks.append("\n".join(lines))
    for tier, header in (("episodic", "[相关事件 Episodic]"),
                         ("concept", "[相关概念 Concept]"),
                         ("subconscious", "[近期原文片段 Subconscious]")):
        group = [c for c in injected if c.tier == tier]
        if group:
            lines = [header]
            for c in group:
                lines.append(f"- {c.content[:200]}")
            blocks.append("\n".join(lines))
    budget = int(scenario.get("budget_chars") or 2000)
    text, _truncated, _used = mrs._apply_token_budget_ex(blocks, budget)
    note = mrs._missing_evidence_notice(query, has_hits=bool(injected), enabled=True)
    return f"{text}{note}"


def _injected_chars(ctx: str) -> int:
    idx = (ctx or "").rfind("\n" + mrs._MISSING_NOTICE_TAG)
    return len(ctx[:idx]) if idx >= 0 else len(ctx or "")


# ── 指标 + 检查 ───────────────────────────────────────────────────────────

def compute_metrics(scenario: Dict[str, Any], injected: List[Any], ctx: str,
                    route: str, latency_ms: float) -> Dict[str, Any]:
    expect = scenario.get("expect") or {}
    hit_ids = [str(x) for x in (expect.get("hit_ids") or [])]
    k = int(expect.get("hit_k", 3) or 3)
    ranked_ids = [str(c.id) for c in injected]
    topk = ranked_ids[:k]
    hit_at_k = (len(set(hit_ids) & set(topk)) / float(len(hit_ids))) if hit_ids else None
    adopted = [str(x) for x in (expect.get("adopted_ids") or [])]
    adoption_rate = None
    if adopted is not None and expect.get("adopted_ids") is not None:
        adoption_rate = (len(set(adopted) & set(ranked_ids)) / float(len(ranked_ids))
                         if ranked_ids else 0.0)
    seed = scenario.get("seed") or {}
    storage_rows = sum(len(seed.get(k2) or []) for k2 in ("concepts", "episodic", "subconscious"))
    return {
        "hit_at_k": hit_at_k,
        "hit_ids": sorted(set(hit_ids) & set(ranked_ids)),
        "missed_ids": sorted(set(hit_ids) - set(ranked_ids)),
        "zero_hit": not bool(ranked_ids),
        "injected_tokens": _injected_chars(ctx),
        "latency_ms": round(float(latency_ms), 3),
        "adoption_rate": adoption_rate,
        "storage_rows": storage_rows,
        "route": route,
    }


def run_checks(scenario: Dict[str, Any], injected: List[Any], ctx: str) -> Dict[str, str]:
    expect = scenario.get("expect") or {}
    injected_ids = {str(c.id) for c in injected}
    checks: Dict[str, str] = {}
    hit_ids = {str(x) for x in (expect.get("hit_ids") or [])}
    if hit_ids:
        checks["required_ids"] = "pass" if hit_ids <= injected_ids else "fail"
    forbidden = {str(x) for x in (expect.get("forbidden_ids") or [])}
    if forbidden:
        checks["forbidden_ids"] = "pass" if not (forbidden & injected_ids) else "fail"
    if "want_zero_hit" in expect:
        checks["zero_hit"] = ("pass" if (not injected_ids) == bool(expect["want_zero_hit"])
                              else "fail")
    if expect.get("expect_missing_marker") is not None:
        has_marker = mrs._MISSING_NOTICE_TAG in (ctx or "")
        checks["missing_marker"] = ("pass"
                                    if has_marker == bool(expect["expect_missing_marker"])
                                    else "fail")
    # W0-2 新增（唯一新增 check）：注入 token 上限断言（CJK 计数合理性锚，B17）。
    # 仅当 expect 显式携带 max_injected_tokens 时激活——既有场景零行为变化。
    if expect.get("max_injected_tokens") is not None:
        checks["max_injected_tokens"] = (
            "pass" if _injected_chars(ctx) <= int(expect["max_injected_tokens"])
            else "fail")
    return checks


# ── 场景执行 + 矩阵汇总 ───────────────────────────────────────────────────

def run_scenario(scenario: Dict[str, Any], arm: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """执行单场景 × 单配置臂（假检索注入；LLM 判定面缺省跳过=UNVERIFIED）。"""
    validate_scenario(scenario)
    arm = dict(arm or DEFAULT_ARM)
    started = time.perf_counter()
    candidates = fake_retrieve(scenario)
    stage0 = build_stage0(scenario)
    injected = select_injected(candidates)
    ctx = assemble_injection(scenario, injected)
    ctx, route = mrs._apply_injection_route(
        str(scenario.get("query") or ""), stage0, injected, ctx,
        enabled=bool(arm.get("provider_routing_enabled", False)))
    latency_ms = (time.perf_counter() - started) * 1000.0
    metrics = compute_metrics(scenario, injected, ctx, route, latency_ms)
    checks = run_checks(scenario, injected, ctx)
    llm_checks = list((scenario.get("expect") or {}).get("llm_checks") or [])
    return {
        "id": str(scenario["id"]),
        "category": str(scenario["category"]),
        "arm": str(arm.get("name") or "baseline"),
        "metrics": metrics,
        "checks": checks,
        "passed": all(v != "fail" for v in checks.values()),
        "llm_verification": "UNVERIFIED" if llm_checks else "NOT_REQUIRED",
        "llm_path": "skipped",
    }


def _mean(vals: List[float]) -> float:
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def summarize(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """指标汇总（hit@k 均值跳过 None——无必中目标的场景不计入）。"""
    def _group(gruns: List[Dict[str, Any]]) -> Dict[str, Any]:
        g_hits = [r["metrics"]["hit_at_k"] for r in gruns
                  if r["metrics"].get("hit_at_k") is not None]
        return {
            "runs": len(gruns),
            "hit_at_k_mean": _mean(g_hits),
            "zero_hit_rate": round(
                sum(1 for r in gruns if r["metrics"].get("zero_hit")) / len(gruns), 4)
            if gruns else 0.0,
            "mean_injected_tokens": _mean([r["metrics"]["injected_tokens"] for r in gruns]),
            "mean_latency_ms": _mean([r["metrics"]["latency_ms"] for r in gruns]),
        }

    summary: Dict[str, Any] = {
        "total_runs": len(runs),
        "passed": sum(1 for r in runs if r.get("passed")),
        "failed": sum(1 for r in runs if not r.get("passed")),
        "hit_at_k_mean": _mean([r["metrics"]["hit_at_k"] for r in runs
                                if r["metrics"].get("hit_at_k") is not None]),
        "zero_hit_rate": (round(sum(1 for r in runs if r["metrics"].get("zero_hit"))
                                / len(runs), 4) if runs else 0.0),
        "mean_injected_tokens": _mean([r["metrics"]["injected_tokens"] for r in runs]),
        "mean_latency_ms": _mean([r["metrics"]["latency_ms"] for r in runs]),
        "llm_unverified_count": sum(1 for r in runs
                                    if r.get("llm_verification") == "UNVERIFIED"),
        "by_category": {},
        "by_arm": {},
    }
    for key, field in (("by_category", "category"), ("by_arm", "arm")):
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for r in runs:
            groups.setdefault(str(r.get(field) or "unknown"), []).append(r)
        summary[key] = {gname: _group(gruns) for gname, gruns in groups.items()}
    return summary
