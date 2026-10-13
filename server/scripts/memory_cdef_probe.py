"""CDEF 修改点运行时召回探针（2026-09-14）。

针对本波改动做端到端核查（真实 DB + 真实检索管线）：
  A. C1 台账：每轮落行 + 字段完整（不存内容）
  B. C1/I2 缓存命中：第二轮 cache_hit=True 且 budget/truncated 随缓存保留
  C. C1 截断标注：真截断时尾部标注存在且零数字
  D. D1/D2 门控关：默认零副作用（跨轮去重集合不增长、D3 门控 False）
  E. E1 指令：开=ctx 头部含指令 / 关=不含
  F. E3 listwise fail-open：超时返回 None（回退语义）
  G. E4 任务记忆：600/200 预算两例
运行: cd backend && ../.venv/bin/python -m scripts.memory_cdef_probe
"""
import asyncio
import re
import sys
import time

from sqlalchemy import text

from app.core.config import get_config
from app.db.database import AsyncSessionLocal

config = get_config()


def _ok(label: str, cond: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return cond


async def _recall(db, uid: str, query: str):
    from app.services import memory_retrieval_service as mrs
    return await mrs.retrieve_with_meta(db, uid, [{"role": "user", "content": query}])


async def _ledger_rows(db, uid: str, query_hash: str, limit: int = 5):
    rows = (await db.execute(text(
        "SELECT query_hash, candidate_ids, tier_scores, budget_chars, injected_chars, "
        "truncated, elapsed_ms, cache_hit FROM memory_recall_log "
        "WHERE user_id=:u AND query_hash=:h ORDER BY created_at DESC LIMIT :l"
    ), {"u": uid, "h": query_hash, "l": limit})).mappings().all()
    return [dict(r) for r in rows]


async def main() -> int:
    from app.services.memory_recall_log_service import query_hash_of
    from app.services.memory_retrieval_service import _TRUNCATION_NOTE, _DEFAULT_USAGE_INSTRUCTION

    failures = 0
    async with AsyncSessionLocal() as db:
        uid = (await db.execute(text(
            "SELECT id FROM users WHERE username='test'"))).scalar()
        if not uid:
            print("test user not found")
            return 1

        # ---- A. 台账落行 + 字段 ----
        q_a = f"CDEF 探针 A 台账字段 {int(time.time())}"
        await _recall(db, uid, q_a)
        await asyncio.sleep(1.0)
        rows = await _ledger_rows(db, uid, query_hash_of([q_a]))
        if _ok("A1 台账落行", len(rows) >= 1, f"rows={len(rows)}"):
            r = rows[0]
            failures += 0 if _ok(
                "A2 字段完整（budget/injected/truncated/elapsed/cache_hit/ids）",
                r["budget_chars"] > 0 and r["injected_chars"] > 0
                and r["elapsed_ms"] >= 0 and r["cache_hit"] is False
                and r["candidate_ids"] is not None,
                f"budget={r['budget_chars']} injected={r['injected_chars']} "
                f"elapsed={r['elapsed_ms']}ms trunc={r['truncated']}") else 1

        # ---- B. 缓存命中（同一 query 连跑两次，不清缓存）----
        q_b = f"CDEF 探针 B 缓存命中 {int(time.time())}"
        ctx1, ids1, top1 = await _recall(db, uid, q_b)
        await asyncio.sleep(0.6)
        ctx2, ids2, top2 = await _recall(db, uid, q_b)
        await asyncio.sleep(1.0)
        rows_b = await _ledger_rows(db, uid, query_hash_of([q_b]))
        hit_flags = [r["cache_hit"] for r in rows_b]
        failures += 0 if _ok(
            "B1 第二轮 cache_hit=True（I2 修复）",
            True in hit_flags and False in hit_flags,
            f"cache_hit 序列(新→旧)={hit_flags}") else 1
        failures += 0 if _ok(
            "B2 命中轮 ctx/ids 与首轮一致", ctx1 == ctx2 and ids1 == ids2,
            f"ids={len(ids1)}→{len(ids2)}") else 1
        hit_row = next((r for r in rows_b if r["cache_hit"]), None)
        if hit_row is not None:
            failures += 0 if _ok(
                "B3 命中轮 budget/truncated 随缓存保留（非 0/False 默认）",
                hit_row["budget_chars"] > 0,
                f"budget={hit_row['budget_chars']} trunc={hit_row['truncated']}") else 1

        # ---- C. 截断标注 ----
        truncated_seen = None
        for q in [
            "请把我关于记忆系统、检索、评测、部署、模型供应商、语音、死磕、"
            "后台任务、笔记、工作区、代码执行、浏览器、MCP 的所有事情都总结一遍",
            "总结我所有的偏好、习惯、工作、项目、技术栈、工具、生活、计划与历史讨论",
        ]:
            ctx, _ids, _top = await _recall(db, uid, q)
            if ctx.endswith(_TRUNCATION_NOTE):
                truncated_seen = (q, ctx)
                break
        if truncated_seen is not None:
            _q, ctx = truncated_seen
            failures += 0 if _ok(
                "C1 截断标注存在且零数字",
                ctx.endswith(_TRUNCATION_NOTE) and not re.search(r"\d", _TRUNCATION_NOTE),
                f"tail={_TRUNCATION_NOTE!r}") else 1
        else:
            print("[SKIP] C1 本轮未触发真截断（预算内），标注逻辑已由单测覆盖")

        # ---- D. 门控关零副作用 ----
        import app.services.memory_retrieval_service as mrs
        import app.services.memory_consolidation_service as mcons
        ret_cfg = config.memory_retrieval
        gate_state = {
            k: ret_cfg.get(k) for k in (
                "text_cross_turn_dedup_enabled", "assembly_mmr_enabled",
                "expansion_rho_enabled", "agpr_enabled",
                "deterministic_edges_enabled", "edge_read_whitelist_enabled")
        }
        failures += 0 if _ok(
            "D1 门控默认全关", all(not v for v in gate_state.values()),
            str(gate_state)) else 1
        failures += 0 if _ok(
            "D2 门控关时跨轮去重集合不增长",
            not mrs._text_injected_ids.get(uid)) else 1
        failures += 0 if _ok(
            "D3 门控关（accessor 读 [memory.retrieval]）",
            mcons._d3_fast_path_enabled() is False and mcons._d3_mst_enabled() is False) else 1

        # ---- E. E1 指令开/关 ----
        saved = ret_cfg.get("injection_usage_instruction_enabled")
        try:
            q_e = f"CDEF 探针 E 指令 {int(time.time())}"
            ret_cfg["injection_usage_instruction_enabled"] = False
            ctx_off, _, _ = await _recall(db, uid, q_e)
            q_e2 = q_e + " 二"
            ret_cfg["injection_usage_instruction_enabled"] = True
            ctx_on, _, _ = await _recall(db, uid, q_e2)
            failures += 0 if _ok(
                "E1 关=无指令 / 开=头部含指令",
                (_DEFAULT_USAGE_INSTRUCTION not in ctx_off)
                and ctx_on.startswith(_DEFAULT_USAGE_INSTRUCTION)) else 1
        finally:
            ret_cfg["injection_usage_instruction_enabled"] = saved

        # ---- F. E3 fail-open ----
        saved_lv = ret_cfg.get("listwise_verifier_enabled")
        try:
            from app.services.memory_listwise_verifier import verify_listwise
            ret_cfg["listwise_verifier_enabled"] = True
            t0 = time.monotonic()
            keep = await verify_listwise(
                uid, "CDEF 探针 F", [{"id": "x", "text": "y"}], timeout_ms=1)
            dt = (time.monotonic() - t0) * 1000
            failures += 0 if _ok(
                "F1 listwise 超时 fail-open（None）", keep is None, f"{dt:.0f}ms") else 1
            ret_cfg["listwise_verifier_enabled"] = False
            keep_off = await verify_listwise(uid, "q", [{"id": "x", "text": "y"}])
            failures += 0 if _ok(
                "F2 门控关零调用（None）", keep_off is None) else 1
        finally:
            ret_cfg["listwise_verifier_enabled"] = saved_lv

        # ---- G. E4 任务记忆 ----
        from app.services.memory_task_context import build_task_memory_context
        for budget in (600, 200):
            block = await build_task_memory_context(db, uid, "我的编辑器偏好", budget_chars=budget)
            failures += 0 if _ok(
                f"G 任务记忆 budget={budget}",
                block.startswith("[任务记忆]") and len(block) <= budget + 40) else 1

    print(f"\n== CDEF 探针结果: {'ALL PASS' if failures == 0 else f'{failures} FAIL'} ==")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
