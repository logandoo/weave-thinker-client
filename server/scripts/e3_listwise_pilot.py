"""E3 listwise 记忆验证器试点（2026-09-14，默认关）。

流程：取 test 用户最近召回台账的候选 id → 构建候选描述 → 开启开关运行一次
listwise 调用（记录延迟/保留数）→ 再以 1ms 超时演示 fail-open 回退。

运行: cd backend && ../.venv/bin/python -m scripts.e3_listwise_pilot
报告: tests/e3_listwise_pilot_20260914.md（只读业务数据；一次真实 LLM 调用）
"""
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from app.core.config import get_config
from app.db.database import AsyncSessionLocal

REPORT = Path(__file__).resolve().parents[2] / "tests" / "e3_listwise_pilot_20260914.md"


async def _candidates(db, uid: str) -> list[dict]:
    row = (await db.execute(text(
        "SELECT candidate_ids FROM memory_recall_log WHERE user_id=:uid "
        "AND candidate_ids IS NOT NULL ORDER BY created_at DESC LIMIT 1"
    ), {"uid": uid})).fetchone()
    ids: list[str] = []
    if row and row[0]:
        try:
            ids = json.loads(row[0])[:8]
        except (TypeError, ValueError):
            ids = []
    if not ids:
        rows = (await db.execute(text(
            "SELECT id FROM memory_concepts WHERE user_id=:uid AND valid_to IS NULL "
            "ORDER BY last_recalled_at DESC NULLS LAST LIMIT 8"
        ), {"uid": uid})).fetchall()
        ids = [r[0] for r in rows]
    out = []
    for cid in ids:
        r = (await db.execute(text(
            "SELECT canonical_name, description_short FROM memory_concepts WHERE id=:id"
        ), {"id": cid})).fetchone()
        if r:
            out.append({"id": cid, "text": f"{r[0]}：{r[1] or ''}"})
        else:
            r2 = (await db.execute(text(
                "SELECT narrative FROM memory_episodes WHERE id=:id"), {"id": cid})).fetchone()
            if r2:
                out.append({"id": cid, "text": (r2[0] or "")[:200]})
    return out


async def main() -> int:
    cfg = get_config().memory_retrieval
    saved = cfg.get("listwise_verifier_enabled")
    lines = [
        "# E3 listwise 记忆验证器试点报告",
        f"- 时间: {datetime.now().isoformat(timespec='seconds')}",
        "- 开关: `[memory.retrieval] listwise_verifier_enabled`（默认关；试点进程内临时开启）",
        "- 试点路径: 脚本直调 `memory_listwise_verifier.verify_listwise`（热路径零改动）",
        "",
    ]
    try:
        async with AsyncSessionLocal() as db:
            uid = (await db.execute(text(
                "SELECT id FROM users WHERE username='test'"))).scalar()
            if not uid:
                print("test user not found")
                return 1
            cands = await _candidates(db, uid)
            lines.append(f"- 候选数: {len(cands)}（来源：最近一轮召回台账 candidate_ids，"
                         "缺失时回退活跃概念）")
            lines.append("")
            lines.append("## 试点 1：真实 LLM listwise（timeout 60s）")
            cfg["listwise_verifier_enabled"] = True
            t0 = time.monotonic()
            from app.services.memory_listwise_verifier import verify_listwise
            keep = await verify_listwise(uid, "总结我的记忆要点", cands, timeout_ms=60000)
            dt = (time.monotonic() - t0) * 1000
            lines.append(f"- 延迟: {dt:.0f}ms；输入 {len(cands)} → 保留 {len(keep) if keep else 0}")
            lines.append(f"- 结果: {keep if keep is not None else 'None（fail-open 回退）'}")
            lines.append("")
            lines.append("## 试点 2：fail-open（timeout 1ms）")
            t0 = time.monotonic()
            keep2 = await verify_listwise(uid, "总结我的记忆要点", cands, timeout_ms=1)
            dt2 = (time.monotonic() - t0) * 1000
            lines.append(f"- 延迟: {dt2:.0f}ms；返回: {keep2!r}（None=回退现有注入，符合预期）")
            lines.append("")
            lines.append("## 结论")
            ok = keep is not None
            lines.append(f"- 真实调用{'成功' if ok else '失败（路由/网络原因，fail-open 生效）'}；"
                         f"超时回退{'已验证' if keep2 is None else '异常'}。")
            lines.append("- 热路径（chat/voice）零改动；装配点接入需另行门控评审（E4 已提供注入点）。")
    finally:
        cfg["listwise_verifier_enabled"] = saved
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"report: {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
