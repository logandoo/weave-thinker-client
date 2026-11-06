#!/usr/bin/env python3
"""
W3 记忆评测/修订报告脚本（M4a 汇总 + M3a 修订候选；离线 report-only）。

M4a：汇总 `tests/memory_evals/run_eval.py` 产出的指标 JSON → markdown
（hit@k / 零命中率 / 注入 token / 延迟 / UNVERIFIED 计数，按场景类别×配置臂）。

M3a（ReHarness 2610.12668 backward revision，**先离线**）：join
memory_recall_log（candidate_ids=**注入集**，A7 勘正）× adoption
（memory_adoption_service 口径）× clarifications ×（可选）死磕步骤结局 →
「疑似误导/过度泛化」候选清单（含源单元引用）→ docs/ markdown 供人工裁定
（edit/merge/forget）。

**Report-only / 纯读**：绝不自动修改记忆（M3b 在线修订需用户单独拍板）。
离线行源可走 `--from-json`（内存/导出行，不连生产库；E4 租户隔离约束）。

用法:
    cd backend && python -m scripts.memory_eval_report --results ../tests/memory_evals/results/latest.json --out ../docs/memory_eval_report.md
    cd backend && python -m scripts.memory_eval_report revision --days 30 --out ../docs/memory_revision_candidates.md
    cd backend && python -m scripts.memory_eval_report revision --from-json rows.json --out -
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, ".")

# ── M3a 规则阈值（可调；方向性阈值，机器判据来自 W0 基线 / M4a 评测台）──────
_MIN_INJECTED_TURNS_NEVER_ADOPTED = 3
_MIN_DISTINCT_QUERIES_GENERALIZED = 3
_MIN_CONVERSATIONS_GENERALIZED = 2
_MAX_ADOPTION_RATE_GENERALIZED = 0.2
_MIN_DEATHMATCH_FAILURE_EXPOSURE = 2

_RULE_WEIGHTS = {
    "clarified": 3.0,
    "never_adopted": 2.0,
    "over_generalized": 2.0,
    "deathmatch_failure": 2.0,
}

_LABEL_MISLEADING = "疑似误导"
_LABEL_OVER_GENERAL = "过度泛化"


def _json_list(value: Any) -> List[Any]:
    """JSON 串/列表双形态 → 列表（损坏 fail-open 空表；asyncpg JSON 列可能已解码）。"""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "[]")
        except (ValueError, TypeError):
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return []


def _unit_id_of(row: Dict[str, Any]) -> str:
    return str(row.get("unit_id") or row.get("concept_id") or row.get("id") or "")


def normalize_recall_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """召回台账行归一（A7 口径：candidate_ids = **注入集**，全候选池字段不参与 join）。"""
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        row = dict(r)
        row["candidate_ids"] = [str(x) for x in _json_list(row.get("candidate_ids"))]
        out.append(row)
    return out


def normalize_adoption_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        row = dict(r)
        row["unit_id"] = _unit_id_of(row)
        row["adopted"] = bool(row.get("adopted", True))
        out.append(row)
    return out


def normalize_clarification_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        row = dict(r)
        row["affected_concept_ids"] = [str(x) for x in _json_list(row.get("affected_concept_ids"))]
        out.append(row)
    return out


def normalize_deathmatch_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """死磕步骤结局行（可选数据集）归一。

    `outcome` 显式给定用显式值；否则按 `verify_failures`/`final_status` 启发映射
    （离线代理口径——步骤级结局不在归档表，见 §10.7 未覆盖）。
    """
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        row = dict(r)
        row["unit_ids"] = [str(x) for x in _json_list(row.get("unit_ids"))]
        outcome = str(row.get("outcome") or "").strip()
        if not outcome:
            try:
                vf = int(row.get("verify_failures") or 0)
            except (TypeError, ValueError):
                vf = 0
            status = str(row.get("final_status") or "")
            outcome = "failed" if (vf > 0 or status in ("failed", "blocked", "stalled")) else "ok"
        row["outcome"] = outcome
        out.append(row)
    return out


def normalize_unit_rows(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows or []:
        uid = _unit_id_of(r)
        if uid:
            out[uid] = {"unit_id": uid, "name": r.get("name"), "tier": r.get("tier")}
    return out


def compute_revision_candidates(rows: Dict[str, Any]) -> List[Dict[str, Any]]:
    """M3a join 逻辑（纯函数）：注入集 × 采纳 × 澄清 ×（可选）死磕结局 → 修订候选。

    入参 `rows` 为数据集映射（缺省键=空）：
    - `recall`：召回台账行（candidate_ids=**注入集**，A7 勘正）；
    - `adoption`：采纳事件行（memory_adoption_service 口径，`adopted` 布尔）；
    - `clarifications`：用户澄清修正行（affected_concept_ids / correction_type）；
    - `deathmatch_steps`：（可选）死磕步骤结局行；
    - `units`：（可选）单元展示名/tier。

    规则（阈值见模块常量；score=触发规则权重和，降序、同分按 unit_id 稳定序）：
    - R1 `clarified`：被澄清修正的注入单元 → 疑似误导（negate/forget 建议 forget）；
    - R2 `never_adopted`：注入 ≥N 轮且零采纳 → 疑似误导/噪声（建议 edit/forget）；
    - R3 `over_generalized`：跨 ≥Q 查询 ∧ ≥C 会话 ∧ 采纳率 ≤t → 过度泛化（建议 merge/edit）；
    - R4 `deathmatch_failure`：关联死磕失败步骤 ≥K 次 → 疑似误导（建议 edit）。

    只统计**被注入过**（injected_turns ≥ 1）的单元——join 语义=「注入了什么」。
    """
    recall = normalize_recall_rows(rows.get("recall") or [])
    adoption = normalize_adoption_rows(rows.get("adoption") or [])
    clarifications = normalize_clarification_rows(rows.get("clarifications") or [])
    dm_steps = normalize_deathmatch_rows(rows.get("deathmatch_steps") or [])
    units = normalize_unit_rows(rows.get("units") or [])

    stats: Dict[str, Dict[str, Any]] = {}
    adopted_counts: Dict[str, int] = defaultdict(int)
    clar_by_unit: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    dm_by_unit: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for r in recall:
        for uid in r.get("candidate_ids") or []:
            st = stats.setdefault(uid, {
                "injected_turns": 0, "recall_ids": [], "query_hashes": set(),
                "conversations": set(),
            })
            st["injected_turns"] += 1
            rid = r.get("id")
            if rid is not None and rid not in st["recall_ids"]:
                st["recall_ids"].append(rid)
            if r.get("query_hash"):
                st["query_hashes"].add(str(r["query_hash"]))
            if r.get("conversation_id"):
                st["conversations"].add(str(r["conversation_id"]))

    for a in adoption:
        uid = a.get("unit_id")
        if uid and a.get("adopted"):
            adopted_counts[uid] += 1

    for cl in clarifications:
        for uid in cl.get("affected_concept_ids") or []:
            clar_by_unit[str(uid)].append(cl)

    # 死磕关联：显式 unit_ids 优先；缺省按会话回填（该会话注入集暴露于失败步骤）
    for step in dm_steps:
        if step.get("outcome") != "failed":
            continue
        target_ids = step.get("unit_ids") or []
        if not target_ids:
            conv = step.get("conversation_id")
            for uid, st in stats.items():
                if conv and conv in st["conversations"]:
                    target_ids.append(uid)
        for uid in target_ids:
            dm_by_unit[str(uid)].append(step)

    candidates: List[Dict[str, Any]] = []
    for uid in sorted(stats.keys()):
        st = stats[uid]
        injected = int(st["injected_turns"])
        if injected < 1:
            continue
        adoption_count = int(adopted_counts.get(uid, 0))
        adoption_rate = adoption_count / float(injected)
        reasons: List[Dict[str, str]] = []
        verdicts: List[str] = []
        labels: List[str] = []

        def _add_verdict(v: str) -> None:
            if v not in verdicts:
                verdicts.append(v)

        def _add_label(lb: str) -> None:
            if lb not in labels:
                labels.append(lb)

        for cl in clar_by_unit.get(uid, []):
            ctype = str(cl.get("correction_type") or "")
            orig = str(cl.get("original_text") or "").strip()[:40]
            detail = f"用户澄清修正（{ctype or 'unknown'}）"
            if orig:
                detail += f": {orig}"
            reasons.append({"code": "clarified", "detail": detail})
            _add_label(_LABEL_MISLEADING)
            _add_verdict("forget" if ctype in ("negate", "forget") else "edit")

        if injected >= _MIN_INJECTED_TURNS_NEVER_ADOPTED and adoption_count == 0:
            reasons.append({
                "code": "never_adopted",
                "detail": f"注入 {injected} 轮、采纳 0 次（高频注入零采纳）",
            })
            _add_label(_LABEL_MISLEADING)
            _add_verdict("edit")
            _add_verdict("forget")

        n_queries = len(st["query_hashes"])
        n_convs = len(st["conversations"])
        if (n_queries >= _MIN_DISTINCT_QUERIES_GENERALIZED
                and n_convs >= _MIN_CONVERSATIONS_GENERALIZED
                and adoption_rate <= _MAX_ADOPTION_RATE_GENERALIZED):
            reasons.append({
                "code": "over_generalized",
                "detail": f"跨 {n_queries} 查询 / {n_convs} 会话注入，采纳率 {adoption_rate:.2f}",
            })
            _add_label(_LABEL_OVER_GENERAL)
            _add_verdict("merge")
            _add_verdict("edit")

        dm_rows = dm_by_unit.get(uid, [])
        if len(dm_rows) >= _MIN_DEATHMATCH_FAILURE_EXPOSURE:
            reasons.append({
                "code": "deathmatch_failure",
                "detail": f"关联死磕失败步骤 {len(dm_rows)} 次",
            })
            _add_label(_LABEL_MISLEADING)
            _add_verdict("edit")

        if not reasons:
            continue

        score = sum(_RULE_WEIGHTS.get(r["code"], 0.0) for r in reasons)
        meta_unit = units.get(uid, {})
        candidates.append({
            "unit_id": uid,
            "name": meta_unit.get("name"),
            "tier": meta_unit.get("tier"),
            "labels": labels,
            "reasons": reasons,
            "verdicts": verdicts,
            "injected_turns": injected,
            "adoption_count": adoption_count,
            "adoption_rate": round(adoption_rate, 3),
            "distinct_queries": n_queries,
            "distinct_conversations": n_convs,
            "deathmatch_failure_exposure": len(dm_rows),
            "score": round(score, 3),
            "source_refs": {
                "recall_ids": list(st["recall_ids"]),
                "clarification_ids": [c.get("id") for c in clar_by_unit.get(uid, [])],
                "deathmatch_step_ids": [s.get("step_id") for s in dm_rows],
            },
        })

    candidates.sort(key=lambda c: (-float(c["score"]), str(c["unit_id"])))
    return candidates


def render_revision_report(candidates: List[Dict[str, Any]], meta: Optional[Dict[str, Any]] = None) -> str:
    """M3a 报告渲染（纯函数）：候选清单 markdown（人工裁定 edit/merge/forget）。"""
    meta = meta or {}
    lines = [
        "# 记忆修订候选报告（M3a，离线人工裁定）",
        "",
        f"- 生成窗口: 近 {meta.get('days', '?')} 天 · 候选数: **{len(candidates)}**",
        "- 口径: memory_recall_log.candidate_ids = **注入集**（A7 勘正）；"
        "join adoption × clarifications ×（可选）死磕步骤结局",
        "- **纯读报告，不自动修改记忆**（M3b 在线修订需用户单独拍板）",
        "",
    ]
    n_mis = sum(1 for c in candidates if _LABEL_MISLEADING in c.get("labels", []))
    n_gen = sum(1 for c in candidates if _LABEL_OVER_GENERAL in c.get("labels", []))
    lines += [
        "## 概览",
        "",
        f"- {_LABEL_MISLEADING}: **{n_mis}** · {_LABEL_OVER_GENERAL}: **{n_gen}**",
        "",
        "## 候选清单（按分数降序）",
        "",
    ]
    if not candidates:
        lines += ["（无候选——本轮无需人工裁定）", ""]
    for i, c in enumerate(candidates, 1):
        name = c.get("name") or ""
        tier = c.get("tier") or ""
        head = f"### {i}. `{c['unit_id']}`"
        if name or tier:
            head += f"（{name or '—'} · {tier or '—'}）"
        lines += [head, ""]
        lines.append(f"- 标签: {' / '.join(c.get('labels') or [])}")
        lines.append(
            f"- 注入/采纳: 注入 {c.get('injected_turns', 0)} 轮 · "
            f"采纳 {c.get('adoption_count', 0)} 次（率 {c.get('adoption_rate', 0)}）· "
            f"跨 {c.get('distinct_queries', 0)} 查询 / {c.get('distinct_conversations', 0)} 会话"
        )
        lines.append("- 理由:")
        for r in c.get("reasons") or []:
            lines.append(f"  - {r.get('code')}: {r.get('detail')}")
        refs = c.get("source_refs") or {}
        lines.append(
            "- 源单元引用: recall="
            f"{refs.get('recall_ids') or []} · clarifications="
            f"{refs.get('clarification_ids') or []} · deathmatch="
            f"{refs.get('deathmatch_step_ids') or []}"
        )
        lines.append(f"- 建议人工裁定: {' | '.join(c.get('verdicts') or [])}")
        lines.append("")
    lines += [
        "## 人工裁定流程（edit / merge / forget）",
        "",
        "1. **edit**：修正单元内容/边界（保留 id，内容落地前需用户确认）；",
        "2. **merge**：与同主题单元合并（走既有 merge_concepts/superseded_by 链）；",
        "3. **forget**：失效或删除单元（negate/forget 类澄清优先）。",
        "",
        "本报告为离线只读产物，**不自动执行任何裁定**；执行请走人工入口。",
        "",
    ]
    return "\n".join(lines)


# ── M4a 指标汇总（run_eval JSON → markdown；纯函数） ──────────────────────

def _mean(vals: List[float]) -> float:
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def _agg_group(gruns: List[Dict[str, Any]]) -> Dict[str, Any]:
    hits = [(r.get("metrics") or {}).get("hit_at_k") for r in gruns]
    hits = [h for h in hits if h is not None]
    return {
        "runs": len(gruns),
        "hit_at_k_mean": _mean(hits),
        "zero_hit_rate": (round(
            sum(1 for r in gruns if (r.get("metrics") or {}).get("zero_hit"))
            / len(gruns), 4) if gruns else 0.0),
        "mean_injected_tokens": _mean(
            [(r.get("metrics") or {}).get("injected_tokens", 0) for r in gruns]),
        "mean_latency_ms": _mean(
            [(r.get("metrics") or {}).get("latency_ms", 0.0) for r in gruns]),
    }


def aggregate_eval_results(results_list: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """M4a 汇总（纯函数）：合并多份 run_eval 指标 JSON → 按类别×配置臂聚合。

    hit@k 均值跳过 None（无必中目标的场景，如缺证据类，不计入 hit@k）。
    """
    runs: List[Dict[str, Any]] = []
    for res in results_list or []:
        runs.extend(res.get("scenarios") or [])
    agg: Dict[str, Any] = {
        "total_runs": len(runs),
        "llm_unverified": sum(1 for r in runs
                              if r.get("llm_verification") == "UNVERIFIED"),
        "hit_at_k_mean": _mean([(r.get("metrics") or {}).get("hit_at_k") for r in runs
                                if (r.get("metrics") or {}).get("hit_at_k") is not None]),
        "zero_hit_rate": (round(
            sum(1 for r in runs if (r.get("metrics") or {}).get("zero_hit"))
            / len(runs), 4) if runs else 0.0),
        "mean_injected_tokens": _mean(
            [(r.get("metrics") or {}).get("injected_tokens", 0) for r in runs]),
        "mean_latency_ms": _mean(
            [(r.get("metrics") or {}).get("latency_ms", 0.0) for r in runs]),
        "by_category": {},
        "by_arm": {},
    }
    for key, field in (("by_category", "category"), ("by_arm", "arm")):
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for r in runs:
            groups.setdefault(str(r.get(field) or "unknown"), []).append(r)
        agg[key] = {gname: _agg_group(gruns) for gname, gruns in groups.items()}
    return agg


def render_eval_report(agg: Dict[str, Any]) -> str:
    """M4a 报告渲染（纯函数）：指标 JSON 汇总 → markdown。"""
    lines = [
        "# 记忆评测报告（M4a，离线 metadata-only）",
        "",
        f"- 汇总运行数: **{agg['total_runs']}** · LLM 判定未验证（UNVERIFIED）: {agg['llm_unverified']}",
        f"- hit@k 均值: {agg['hit_at_k_mean']} · 零命中率: {agg['zero_hit_rate']} · "
        f"注入 token 均值: {agg['mean_injected_tokens']} · 延迟均值(ms): {agg['mean_latency_ms']}",
        "",
        "## 按场景类别",
        "",
        "| 类别 | 运行数 | hit@k | 零命中率 | 注入 token | 延迟(ms) |",
        "|---|---|---|---|---|---|",
    ]
    for cat in sorted(agg.get("by_category") or {}):
        row = agg["by_category"][cat]
        lines.append(
            f"| {cat} | {row['runs']} | {row['hit_at_k_mean']} | {row['zero_hit_rate']} "
            f"| {row['mean_injected_tokens']} | {row['mean_latency_ms']} |")
    lines += [
        "",
        "## 按配置臂",
        "",
        "| 配置臂 | 运行数 | hit@k | 零命中率 | 注入 token | 延迟(ms) |",
        "|---|---|---|---|---|---|",
    ]
    for arm in sorted(agg.get("by_arm") or {}):
        row = agg["by_arm"][arm]
        lines.append(
            f"| {arm} | {row['runs']} | {row['hit_at_k_mean']} | {row['zero_hit_rate']} "
            f"| {row['mean_injected_tokens']} | {row['mean_latency_ms']} |")
    lines += [
        "",
        "> LLM 判定面缺省跳过：凡依赖 LLM 判定的场景检查标记 UNVERIFIED，不计入 hit@k；",
        "> 指标层为假检索结果注入（metadata-only），延迟为评测台执行耗时，非线上检索延迟。",
        "",
    ]
    return "\n".join(lines)


# ── M3a 数据装载（DB 只读 / JSON 离线行源） ────────────────────────────────

async def _load_rows_db(days: int, user_id: Optional[str] = None) -> Dict[str, Any]:
    """生产库只读装载（SELECT-only；E4：不写、不迁移、可 --from-json 全离线替代）。

    fix-round E4：`--user-id` 按用户过滤（recall/clarifications 走 user_id 列；
    deathmatch_harness_runs 经 conversations 子查询收口）。

    adoption 派生自 memory_adoption_service.match_adopted_concepts 口径：
    注入集概念名/别名在后续助手回答中出现 = 采纳事件（回答文本在内存中匹配，
    **不落盘**、不进报告——报告只带 id 级源引用）。
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    db_url = os.environ.get("CLEANUP_DB_URL")
    if not db_url:
        from app.core.config import get_config
        db_url = get_config().database_url
    engine = create_async_engine(db_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    _params: Dict[str, Any] = {"d": int(days)}
    _user_sql = ""
    if user_id:
        _params["uid"] = str(user_id)
        _user_sql = " AND user_id = :uid"
    _dm_user_sql = (
        " AND conversation_id IN (SELECT id FROM conversations WHERE user_id = :uid)"
        if user_id else ""
    )
    try:
        async with sm() as s:
            res = await s.execute(text(
                "SELECT id, user_id, conversation_id, query_hash, candidate_ids, created_at "
                "FROM memory_recall_log "
                "WHERE created_at >= NOW() - make_interval(days => :d)"
                f"{_user_sql} "
                "ORDER BY created_at ASC"
            ), _params)
            recall = [dict(r._mapping) for r in res]

            res = await s.execute(text(
                "SELECT id, user_id, affected_concept_ids, correction_type, "
                "original_text, created_at FROM memory_clarifications "
                "WHERE created_at >= NOW() - make_interval(days => :d)"
                f"{_user_sql} "
                "ORDER BY created_at ASC"
            ), _params)
            clarifications = [dict(r._mapping) for r in res]

            res = await s.execute(text(
                "SELECT conversation_id, final_status, verify_failures, created_at "
                "FROM deathmatch_harness_runs "
                "WHERE created_at >= NOW() - make_interval(days => :d)"
                f"{_dm_user_sql} "
                "ORDER BY created_at ASC"
            ), _params)
            deathmatch = [dict(r._mapping) for r in res]

            injected_ids = sorted({uid for r in recall
                                   for uid in _json_list(r.get("candidate_ids"))})
            concepts: List[Dict[str, Any]] = []
            if injected_ids:
                res = await s.execute(text(
                    "SELECT id, canonical_name, aliases, memory_type FROM memory_concepts "
                    "WHERE id = ANY(:ids)"
                ), {"ids": injected_ids})
                concepts = [dict(r._mapping) for r in res]

            conv_ids = sorted({str(r.get("conversation_id")) for r in recall
                               if r.get("conversation_id")})
            answers: List[Dict[str, Any]] = []
            if conv_ids:
                res = await s.execute(text(
                    "SELECT conversation_id, content, created_at FROM messages "
                    "WHERE role = 'assistant' AND conversation_id = ANY(:cids) "
                    "ORDER BY created_at ASC"
                ), {"cids": conv_ids})
                answers = [dict(r._mapping) for r in res]
    finally:
        await engine.dispose()

    from app.services.memory_adoption_service import match_adopted_concepts
    concept_rows = [{"id": c.get("id"), "canonical_name": c.get("canonical_name"),
                     "aliases": c.get("aliases")} for c in concepts]
    answers_by_conv: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for a in answers:
        answers_by_conv[str(a.get("conversation_id"))].append(a)

    adoption: List[Dict[str, Any]] = []
    for r in recall:
        conv = str(r.get("conversation_id") or "")
        ts = r.get("created_at")
        injected = [str(x) for x in _json_list(r.get("candidate_ids"))]
        if not injected:
            continue
        picked = None
        for a in answers_by_conv.get(conv, []):
            if ts is None or (a.get("created_at") is not None and a["created_at"] >= ts):
                picked = a
                break
        if not picked:
            continue
        for cid in match_adopted_concepts(concept_rows, picked.get("content") or ""):
            if cid in injected:
                adoption.append({"unit_id": cid, "adopted": True,
                                 "conversation_id": conv, "created_at": picked.get("created_at")})

    units = [{"unit_id": c.get("id"), "name": c.get("canonical_name"),
              "tier": c.get("memory_type") or "concept"} for c in concepts]
    return {
        "recall": recall,
        "adoption": adoption,
        "clarifications": clarifications,
        "deathmatch_steps": deathmatch,
        "units": units,
    }


# ── CLI ────────────────────────────────────────────────────────────────────

def _load_results_json(paths: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in paths:
        with open(p, "r", encoding="utf-8") as f:
            out.append(json.load(f))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "revision":
        return _main_revision(argv[1:])
    return _main_eval(argv)


def _write_out(out_path: str, text: str) -> None:
    if out_path == "-":
        sys.stdout.write(text)
        return
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"written: {out_path}")


def _main_revision(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="memory_eval_report revision",
        description="M3a 离线修订候选报告（纯读；人工裁定 edit/merge/forget）")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--from-json", dest="from_json", default=None,
                        help="离线行源 JSON（recall/adoption/clarifications/deathmatch_steps/units）")
    parser.add_argument("--user-id", dest="user_id", default=None,
                        help="DB 模式按用户过滤（M3a 单用户审计；--from-json 时忽略）")
    parser.add_argument("--out", default="-", help="markdown 输出路径（- = stdout）")
    args = parser.parse_args(argv)

    if args.from_json:
        with open(args.from_json, "r", encoding="utf-8") as f:
            rows = json.load(f)
    else:
        import asyncio
        rows = asyncio.run(_load_rows_db(args.days, user_id=args.user_id))

    candidates = compute_revision_candidates(rows or {})
    md = render_revision_report(candidates, {"days": args.days})
    _write_out(args.out, md)
    return 0


def _main_eval(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="memory_eval_report",
        description="M4a 记忆评测报告：汇总 run_eval 指标 JSON → markdown")
    parser.add_argument("--results", nargs="+", required=True,
                        help="run_eval 产出的指标 JSON（一份或多份）")
    parser.add_argument("--out", default="-", help="markdown 输出路径（- = stdout）")
    args = parser.parse_args(argv)
    results = _load_results_json(args.results)
    md = render_eval_report(aggregate_eval_results(results))
    _write_out(args.out, md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
