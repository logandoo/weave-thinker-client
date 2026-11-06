#!/usr/bin/env python3
"""
B4 (report-only v1, 2026-08-30): deathmatch harness 复盘报告.

读取 deathmatch_harness_runs（终态归档）+ conversations 死磕字段，聚合：
- 终态分布 / 平均轮数 / 停滞直方图
- verifier 复发问题类（≥3 次 = harness 变更候选）
- 单组件有界变更提案模板 + train/dev 验收门清单（AutoDesign 双集隔离 +
  JIT-Agent 三轴准则：reward↑ 且 (latency↓ 或 cost↓) 且其他维度不降）

**Report-only**：只出报告与提案，绝不自动修改 harness——归档数据未积累到
统计意义前，自动外环没有依据（用户裁决 2026-08-30）。

用法:
    cd backend && python -m scripts.deathmatch_harness_report [--days 30] [--out ../tests/deathmatch_evals/harness_report.md]
    CLEANUP_DB_URL="postgresql+asyncpg://..." python -m scripts.deathmatch_harness_report
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, Iterable, List

sys.path.insert(0, ".")

_RECURRENCE_MIN = 3


def aggregate_harness_runs(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure aggregation over archive rows (no I/O).

    Returns: {total, status_counts, avg_turns_by_status, stall_histogram,
    issue_recurrence (class→count, sorted desc, ≥_RECURRENCE_MIN flagged),
    config_variants, event_type_counts, correction_mix}.

    D3（论文 2609.26261 CASD，2026-10-13 W1）：统计清单补 B4——
    修正方式分布（node_recovery 的 local_retry/local_patch/replan）与
    事件类型分布（evidence_gate_block/step_stuck_bypass/consensus_challenge…），
    供 LLM 规则蒸馏与人工裁定。
    """
    rows = list(rows or [])
    status_counts: Counter = Counter()
    turns_by_status: Dict[str, List[int]] = {}
    stall_hist: Counter = Counter()
    issue_classes: Counter = Counter()
    config_variants: Counter = Counter()
    event_types: Counter = Counter()
    correction_mix: Counter = Counter()
    tool_totals = {"total_calls": 0, "redundant_calls": 0, "error_results": 0}
    tool_top: Counter = Counter()
    for r in rows:
        status = str(r.get("final_status") or "unknown")
        status_counts[status] += 1
        turns_by_status.setdefault(status, []).append(int(r.get("turns") or 0))
        stall_hist[int(r.get("verify_failures") or 0)] += 1
        # issue classes: the archive stores goal; issue text comes from the
        # decision JSON when present (column `issues_json` optional).
        for iss in (r.get("issues") or []):
            cls = str(iss)[:40].strip()
            if cls:
                issue_classes[cls] += 1
        for ev in (r.get("events") or []):
            if not isinstance(ev, dict):
                continue
            et = str(ev.get("type") or "")[:60]
            if et:
                event_types[et] += 1
            # AED 四元组口径（§4.1 折入 D3）：correction_kind 优先，
            # node_recovery 回落 action——同值不双计。
            if et == "node_recovery":
                action = str(ev.get("correction_kind") or ev.get("action") or "")[:30]
                if action:
                    correction_mix[action] += 1
            elif ev.get("correction_kind"):
                correction_mix[str(ev.get("correction_kind"))[:30]] += 1
        ts = r.get("tool_stats") or {}
        for k in tool_totals:
            tool_totals[k] += int(ts.get(k) or 0)
        for t in (ts.get("top_repeated") or []):
            if isinstance(t, dict):
                tool_top[str(t.get("name") or "")] += int(t.get("count") or 0)
        try:
            cfg = json.loads(r.get("config_json") or "{}")
            key = tuple(sorted((k, str(v)) for k, v in cfg.items()))
        except Exception:
            key = ()
        config_variants[key] += 1
    avg_turns = {
        s: (sum(ts) / len(ts) if ts else 0.0) for s, ts in turns_by_status.items()
    }
    recurrent = [
        {"issue_class": c, "count": n, "harness_candidate": n >= _RECURRENCE_MIN}
        for c, n in issue_classes.most_common(20)
    ]
    return {
        "total": len(rows),
        "status_counts": dict(status_counts),
        "avg_turns_by_status": {s: round(v, 1) for s, v in avg_turns.items()},
        "stall_histogram": dict(sorted(stall_hist.items())),
        "issue_recurrence": recurrent,
        "config_variants": len(config_variants),
        "event_type_counts": dict(event_types.most_common(20)),
        "correction_mix": dict(correction_mix.most_common(20)),
        "tool_metrics": dict(tool_totals),
        "tool_top_repeated": dict(tool_top.most_common(10)),
    }


def render_report(agg: Dict[str, Any], *, days: int) -> str:
    lines = [
        f"# Deathmatch Harness 复盘报告（近 {days} 天，report-only v1）",
        "",
        f"- 归档目标数: **{agg['total']}**",
        f"- 终态分布: {agg['status_counts']}",
        f"- 平均轮数: {agg['avg_turns_by_status']}",
        f"- 停滞直方图（verify_failures→频次）: {agg['stall_histogram']}",
        f"- 配置变体数: {agg['config_variants']}",
        "",
        "## 复发问题类（≥3 = harness 变更候选）",
        "",
        "| 问题类 | 次数 | 候选 |",
        "|---|---|---|",
    ]
    for it in agg["issue_recurrence"]:
        lines.append(f"| {it['issue_class']} | {it['count']} | {'✅' if it['harness_candidate'] else ''} |")
    if not agg["issue_recurrence"]:
        lines.append("| （无记录） | 0 |  |")
    lines += [
        "",
        "## 修正方式分布（D3/B4：哪类修正 × 复发率）",
        "",
    ]
    if agg.get("correction_mix"):
        for k, v in agg["correction_mix"].items():
            lines.append(f"- {k}: {v}")
    else:
        lines.append("- （无记录）")
    lines += ["", "## 事件类型分布", ""]
    if agg.get("event_type_counts"):
        for k, v in agg["event_type_counts"].items():
            lines.append(f"- {k}: {v}")
    else:
        lines.append("- （无记录）")
    tm = agg.get("tool_metrics") or {}
    lines += [
        "",
        "## 重复工具调用 / 工具错误度量（B3/B4，D-20 部分落实）",
        "",
        f"- 工具调用总数: {tm.get('total_calls', 0)}",
        f"- 冗余重复调用（同签名 n 次计 n−1）: {tm.get('redundant_calls', 0)}",
        f"- 工具错误结果数: {tm.get('error_results', 0)}",
    ]
    if agg.get("tool_top_repeated"):
        lines.append("- 重复签名 top: " + ", ".join(
            f"{k}×{v}" for k, v in agg["tool_top_repeated"].items()))
    lines += [
        "",
        "## 单组件有界变更提案（模板——人工填写，report-only 不自动改 harness）",
        "",
        "- 目标组件（五选一）: 提示词 / 工具编排 / 阈值配置 / 校验维度 / 反馈注入",
        "- 针对复发类: <从上方候选选 1 个>",
        "- 变更内容（单组件、有界、可回滚）: <…>",
        "",
        "## 验收门（AutoDesign 双集隔离 + JIT 三轴）",
        "",
        "1. 26 场景基线拆 train/dev **显式不相交**两集（评审记录隔离声明）",
        "2. 变更后: train 集判定准确率↑ **且** dev 集不降",
        "3. 三轴同报: reward / latency / cost——reward↑ 时 latency 或 cost 至少一项严格更优，其余不降",
        "4. 全程真实运行证据（exit-code 套件 + 必要时 Mode B 重跑）",
    ]
    return "\n".join(lines) + "\n"


def compute_tool_stats(calls_raw: Any, results_raw: Any) -> Dict[str, Any]:
    """B4（论文漏采 B3/B4 补入 D3）：冗余工具调用/工具错误度量（纯函数）。

    calls_raw/results_raw = messages.tool_calls / tool_results（**生产形状**：
    _load_rows 逐消息攒出的 list[str|None]，元素是该消息 JSON 文本；或单条
    JSON 文本；或已解码 list/dict）。
    fix-round B4：`_load` 改**展平器**——str→json.loads；list 元素逐个
    （str→loads、dict→直用）；tool_results 的 {"agent_steps": [...]} 包装剥开。
    错误识别=条目 error/is_error 键**或**条目 result（JSON 串/字典）内含
    "error": true。
    `redundant_calls`（原 repeated_calls）= **冗余口径** sum(n-1 for n≥2)：
    同签名 n 次调用有 n-1 次是冗余的。"""
    def _load(x):
        """生产形状展平器（fix-round B4）。"""
        items = x
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except Exception:
                return []
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            return []
        out = []
        for el in items:
            if isinstance(el, str):
                try:
                    el = json.loads(el)
                except Exception:
                    continue
            if isinstance(el, dict):
                steps = el.get("agent_steps")
                if isinstance(steps, list):
                    out.extend(s for s in steps if isinstance(s, dict))
                else:
                    out.append(el)
            elif isinstance(el, list):
                out.extend(s for s in el if isinstance(s, dict))
        return out

    def _entry_is_error(entry: dict) -> bool:
        """条目错误=error/is_error 键 **或** result（JSON 串/字典）内 "error": true。"""
        if entry.get("error") or entry.get("is_error"):
            return True
        res = entry.get("result")
        if isinstance(res, str):
            s = res.strip()
            if not s:
                return False
            try:
                res = json.loads(s)
            except Exception:
                return False
        return bool(isinstance(res, dict) and (res.get("error") or res.get("is_error")))

    calls = _load(calls_raw)
    results = _load(results_raw)
    sig_counts: Counter = Counter()
    total = 0
    for c in calls:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") if isinstance(c.get("function"), dict) else {}
        name = str(c.get("name") or fn.get("name") or "")[:60]
        args = c.get("arguments", fn.get("arguments", ""))
        if isinstance(args, (dict, list)):
            args = json.dumps(args, sort_keys=True, ensure_ascii=False)
        sig = f"{name}|{hashlib.sha1(str(args).encode('utf-8')).hexdigest()[:12]}"
        sig_counts[sig] += 1
        total += 1
    # 冗余口径（fix-round Minors）：同签名 n 次调用 = n-1 条冗余。
    redundant = sum(n - 1 for n in sig_counts.values() if n >= 2)
    errors = 0
    for r in results:
        if isinstance(r, dict) and _entry_is_error(r):
            errors += 1
    top = []
    for sig, n in sig_counts.most_common(5):
        if n >= 2:
            top.append({"name": sig.split("|", 1)[0], "count": n})
    return {
        "total_calls": total,
        "redundant_calls": int(redundant),
        "error_results": int(errors),
        "top_repeated": top,
    }


def _unpack_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """纯拆包缝（可单测）：issues_json/events_json → issues/events 列表。

    C1（复审）：events 是**真实生产形状**——来自 conversations.deathmatch_events
    （归档会话的决策/事件日志，cap 50），不是测试伪造键。"""
    for r in rows:
        try:
            r["issues"] = json.loads(r.pop("issues_json", None) or "[]")
        except Exception:
            r["issues"] = []
        raw_ev = r.pop("events_json", None)
        try:
            # NEW-1（复审实证）：asyncpg 对 JSON 列自动解码为 list，text()
            # 取回的不一定是 JSON 字符串——双形态兼容，勿吞。
            if isinstance(raw_ev, str):
                ev = json.loads(raw_ev or "[]")
            else:
                ev = raw_ev or []
            r["events"] = ev if isinstance(ev, list) else []
        except Exception:
            r["events"] = []
    return rows


async def _load_rows(days: int) -> List[Dict[str, Any]]:
    db_url = os.environ.get("CLEANUP_DB_URL")
    if not db_url:
        from app.core.config import get_config
        cfg = get_config()
        db_url = cfg.database_url
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    engine = create_async_engine(db_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        res = await s.execute(text(
            "SELECT h.conversation_id, h.final_status, h.turns, h.verify_failures, "
            "h.plan_steps, h.plan_done, h.judge_model, h.verify_model, h.config_json, h.goal, "
            "h.created_at, h.issues_json, c.deathmatch_events AS events_json "
            "FROM deathmatch_harness_runs h "
            "LEFT JOIN conversations c ON c.id = h.conversation_id "
            f"WHERE h.created_at >= NOW() - INTERVAL '{int(days)} days' "
            "ORDER BY h.created_at DESC"
        ))
        rows = [dict(r._mapping) for r in res]
        # B4：归档会话的工具调用/结果（messages 工具列）→ 逐会话统计
        conv_ids = [r.get("conversation_id") for r in rows if r.get("conversation_id")]
        tool_map: Dict[str, Dict[str, Any]] = {}
        if conv_ids:
            res2 = await s.execute(text(
                "SELECT conversation_id, tool_calls, tool_results FROM messages "
                "WHERE conversation_id = ANY(:ids)"), {"ids": conv_ids})
            acc: Dict[str, Dict[str, Any]] = {}
            for m in res2:
                mid = m[0]
                acc.setdefault(mid, {"calls": [], "results": []})
                acc[mid]["calls"].append(m[1])
                acc[mid]["results"].append(m[2])
            for mid, blob in acc.items():
                tool_map[mid] = compute_tool_stats(blob["calls"], blob["results"])
        for r in rows:
            r["tool_stats"] = tool_map.get(r.get("conversation_id")) or compute_tool_stats(None, None)
    await engine.dispose()
    # C1（复审）：events 真源 = conversations.deathmatch_events（join 取得）
    return _unpack_rows(rows)


def render_distill_proposal(agg: Dict[str, Any], rules: List[Dict[str, str]], *, days: int) -> str:
    """D3：蒸馏提案文档（docs/proposals/，人工门——绝不自动改 harness）。

    rules = [{"pattern": ..., "proposed_rule": ..., "evidence": ...}]；
    LLM 不可用时 rules 为空 → 只落统计清单（fail-open）。
    """
    lines = [
        f"# 死磕规则蒸馏提案（近 {days} 天，D3 离线蒸馏——人工门，勿直接改 harness）",
        "",
        f"- 归档目标数: {agg['total']}",
        f"- 复发问题类（≥{_RECURRENCE_MIN} = 候选）:",
        "",
        "| 问题类 | 次数 |",
        "|---|---|",
    ]
    for it in agg.get("issue_recurrence") or []:
        lines.append(f"| {it['issue_class']} | {it['count']} |")
    if not agg.get("issue_recurrence"):
        lines.append("| （无记录） | 0 |")
    lines += ["", "## 修正方式分布", ""]
    for k, v in (agg.get("correction_mix") or {}).items():
        lines.append(f"- {k}: {v}")
    if not agg.get("correction_mix"):
        lines.append("- （无记录）")
    lines += ["", "## 蒸馏规则候选（LLM 产出，须人工裁定后入库）", ""]
    if rules:
        for i, r in enumerate(rules, 1):
            lines += [
                f"### 候选 {i}",
                f"- 复发模式: {r.get('pattern', '')}",
                f"- 提议规则: {r.get('proposed_rule', '')}",
                f"- 证据: {r.get('evidence', '')}",
                "",
            ]
    else:
        lines.append("（LLM 蒸馏未运行/不可用——仅统计清单；可重跑 --distill）")
    lines += [
        "",
        "## 人工门（进入 harness 前必过）",
        "",
        "1. 规则与复发证据一一对应（不得泛化到未观察模式）",
        "2. 单组件、有界、可回滚；写入对应提示词/校验维度后跑 26 场景 train/dev 双集验收",
        "3. 禁自动替换提示词（2026-10-13 复核裁决：CASD 自动替换改人工门）",
    ]
    return "\n".join(lines) + "\n"


async def _distill_rules(agg: Dict[str, Any]) -> List[Dict[str, str]]:
    """LLM 规则蒸馏（fail-open：失败/无候选 → []，提案仍落统计清单）。"""
    candidates = [
        it for it in (agg.get("issue_recurrence") or [])
        if it.get("harness_candidate")
    ]
    if not candidates:
        return []
    try:
        from app.services.agentic_judge import judge_json
        content = json.dumps({
            "issue_recurrence": candidates,
            "correction_mix": agg.get("correction_mix") or {},
        }, ensure_ascii=False)
        parsed = await judge_json(
            "你是死磕 harness 规则蒸馏器。给定复发问题类与修正方式分布，"
            "输出可人工裁定的规则候选（单组件、有界、可回滚）。只输出JSON："
            '{"rules": [{"pattern": "...", "proposed_rule": "...", "evidence": "..."}]}',
            content,
            task="harness_distill",
            default=None,
            timeout=60.0,
        )
        if isinstance(parsed, dict) and isinstance(parsed.get("rules"), list):
            return [
                {
                    "pattern": str(r.get("pattern") or "")[:200],
                    "proposed_rule": str(r.get("proposed_rule") or "")[:400],
                    "evidence": str(r.get("evidence") or "")[:200],
                }
                for r in parsed["rules"][:8] if isinstance(r, dict)
            ]
    except Exception as exc:
        print(f"distill failed (fail-open, stats-only): {exc}")
    return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default="")
    ap.add_argument("--distill", action="store_true",
                    help="D3: 追加 LLM 规则蒸馏 → docs/proposals/deathmatch_distill_<date>.md（人工门）")
    args = ap.parse_args()
    rows = asyncio.run(_load_rows(args.days))
    agg = aggregate_harness_runs(rows)
    report = render_report(agg, days=args.days)
    out = args.out or os.path.join(
        os.path.dirname(__file__), "..", "..", "tests", "deathmatch_evals", "harness_report.md")
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(f"report written: {out} ({agg['total']} runs)")
    if args.distill:
        rules = asyncio.run(_distill_rules(agg))
        from datetime import date
        prop = os.path.join(
            os.path.dirname(__file__), "..", "..", "docs", "proposals",
            f"deathmatch_distill_{date.today().isoformat()}.md")
        prop = os.path.abspath(prop)
        os.makedirs(os.path.dirname(prop), exist_ok=True)
        with open(prop, "w", encoding="utf-8") as fh:
            fh.write(render_distill_proposal(agg, rules, days=args.days))
        print(f"distill proposal written: {prop} ({len(rules)} rules, human gate)")


if __name__ == "__main__":
    main()
