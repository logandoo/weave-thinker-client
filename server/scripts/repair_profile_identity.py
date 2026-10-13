"""W12 Dx-1：画像身份事实修复器（60 生产数据手术）。

背景（08-24 诊断根因 4/6 + 2026-10-08 生产取证）：
- consolidation 曾把 `profile_fact:` 出处当零证据 nightly 误杀画像行
  （valid_to 置位）——正确事实（如「用户是年年的妈妈」）被杀出注入面；
- 提取层曾把助手/产品名（罗航）误归为用户姓名（「用户姓名为罗航」）。
代码层修复（误杀+复活）已随 W12 波落地；本脚本修复**存量数据**。

能力（全部幂等；默认 dry-run，`--execute` 才落库）：
- list：列出该用户全部画像概念（活体+已失效），供人工裁定。
- resurrect：复活指定 id（valid_to=NULL, needs_review=FALSE；仅 source_type='profile_sync'
  且 status='active' 的行，守卫合并行不误复活）。
- supersede：复活并改写描述（修主体归属错误，如「用户姓名为罗航」→正确表述）。
- anchor： upsert 一条画像锚点事实（name 键幂等：存在同名行则复活+更新描述，
  否则 create_concept 新建，自动 embedding/BM25）。

用法（在 60 箱 backend/ 下，venv 内；默认 dry-run，`--execute` 落库）：
  python -m scripts.repair_profile_identity --user YearDoo list
  python -m scripts.repair_profile_identity --user YearDoo resurrect <uuid> --execute
  python -m scripts.repair_profile_identity --user alice supersede <uuid> --set-desc "…" --execute
  python -m scripts.repair_profile_identity --user YearDoo anchor --name "家庭成员关系" --desc "蛮蛮是弟弟（2024年11月出生），年年是姐姐（约5岁）；用户是两个孩子的妈妈。" --execute
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from sqlalchemy import text

from app.services.memory_profile_service import _PROFILE_SOURCE  # 'profile_sync'

logger = logging.getLogger("repair_profile_identity")


def _merge_raw_ids(existing: str | None, key: str) -> list[str]:
    """A-M4：append-if-missing（空/坏 JSON 兜底为 [key]），防 nightly flap。"""
    ids: list = []
    if existing:
        try:
            v = json.loads(existing)
            ids = v if isinstance(v, list) else [existing]
        except (json.JSONDecodeError, TypeError):
            ids = [existing]
    if key not in ids:
        ids.append(key)
    return ids


async def _resolve_user_id(db, username: str) -> str | None:
    r = await db.execute(text("SELECT id FROM users WHERE username = :u"), {"u": username})
    row = r.fetchone()
    return str(row[0]) if row else None


async def list_profile_concepts(db, user_id: str) -> list[dict]:
    r = await db.execute(
        text("""
            SELECT id, canonical_name, description_short, status, valid_to, needs_review,
                   source_raw_ids, updated_at
            FROM memory_concepts
            WHERE user_id = :uid AND source_type = :st
            ORDER BY valid_to IS NULL DESC, updated_at DESC
        """),
        {"uid": user_id, "st": _PROFILE_SOURCE},
    )
    out = []
    for row in r.fetchall():
        out.append({
            "id": str(row[0]), "name": row[1], "desc": row[2],
            "status": row[3], "valid_to": str(row[4]) if row[4] else None,
            "needs_review": row[5], "raw_ids": row[6], "updated_at": str(row[7]),
        })
    return out


async def resurrect_concept(db, user_id: str, concept_id: str, new_desc: str | None = None) -> str:
    """复活画像行。返回 applied|skipped:*。守卫：仅 profile + active 行可复活。"""
    r = await db.execute(
        text("""
            SELECT id, status, valid_to FROM memory_concepts
            WHERE id = :cid AND user_id = :uid AND source_type = :st
        """),
        {"cid": concept_id, "uid": user_id, "st": _PROFILE_SOURCE},
    )
    row = r.fetchone()
    if not row:
        return "skipped:not-profile-or-missing"
    if row[1] != "active":
        return f"skipped:status={row[1]}"  # merged 等不复活（避免与合并目标重复）
    if row[2] is None and new_desc is None:
        return "skipped:already-live"
    if new_desc is None:
        await db.execute(
            text("UPDATE memory_concepts SET valid_to = NULL, needs_review = FALSE, updated_at = NOW() WHERE id = :cid"),
            {"cid": concept_id},
        )
    else:
        await db.execute(
            text("""UPDATE memory_concepts
                    SET valid_to = NULL, needs_review = FALSE, updated_at = NOW(),
                        description_short = :d, description_full = :d
                    WHERE id = :cid"""),
            {"d": new_desc, "cid": concept_id},
        )
        from app.services.memory_concept_service import update_concept_description
        await update_concept_description(db, concept_id, new_desc, new_desc)
    return "applied"


async def upsert_anchor(db, user_id: str, name: str, desc: str) -> str:
    """锚点事实 upsert（name 键幂等）。返回 created|resurrected|updated。"""
    from app.services.memory_concept_service import create_concept, update_concept_description

    r = await db.execute(
        text("""
            SELECT id, valid_to, source_raw_ids FROM memory_concepts
            WHERE user_id = :uid AND source_type = :st AND canonical_name = :nm
              AND status = 'active'
        """),
        {"uid": user_id, "nm": name, "st": _PROFILE_SOURCE},
    )
    row = r.fetchone()
    key = f"profile_fact:{name}"
    if row:
        cid = str(row[0])
        await db.execute(
            text("""UPDATE memory_concepts
                    SET valid_to = NULL, needs_review = FALSE, updated_at = NOW(),
                        description_short = :d, description_full = :d
                    WHERE id = :cid"""),
            {"d": desc, "cid": cid},
        )
        merged = _merge_raw_ids(row[2] if len(row) > 2 else None, key)
        await db.execute(
            text("UPDATE memory_concepts SET source_raw_ids = :k WHERE id = :cid"),
            {"k": json.dumps(merged, ensure_ascii=False), "cid": cid},
        )
        await update_concept_description(db, cid, desc, desc)
        return "resurrected" if row[1] is not None else "updated"
    cid = await create_concept(
        db, user_id, name, desc, desc,
        source_trust="user_stated", memory_type="profile", source_type=_PROFILE_SOURCE,
    )
    if not cid:
        return "skipped:create-failed"
    await db.execute(
        text("UPDATE memory_concepts SET source_raw_ids = :k WHERE id = :cid"),
        {"k": json.dumps([key], ensure_ascii=False), "cid": str(cid)},
    )
    return "created"


async def _amain(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from app.db.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        user_id = await _resolve_user_id(db, args.user)
        if not user_id:
            print(f"USER NOT FOUND: {args.user}")
            return 2
        print(f"user={args.user} id={user_id} execute={args.execute}")

        if args.action == "list":
            rows = await list_profile_concepts(db, user_id)
            print(f"profile concepts: {len(rows)}")
            for r in rows:
                mark = "LIVE" if r["valid_to"] is None else f"KILLED@{r['valid_to'][:16]}"
                print(f"  [{mark}] {r['id'][:8]} status={r['status']} nr={r['needs_review']} | {r['name']} | {(r['desc'] or '')[:90]}")
            return 0

        if args.action == "resurrect" or args.action == "supersede":
            desc = args.set_desc if args.action == "supersede" else None
            if args.action == "supersede" and not desc:
                print("--set-desc required for supersede")
                return 2
            if not args.execute:
                rows = await list_profile_concepts(db, user_id)
                hit = [r for r in rows if r["id"].startswith(args.concept_id)]
                print("DRY-RUN target:", json.dumps(hit, ensure_ascii=False, indent=1))
                print(f"DRY-RUN would {'supersede+resurrect' if desc else 'resurrect'} — rerun with --execute")
                return 0
            result = await resurrect_concept(db, user_id, args.concept_id, desc)
            print(f"resurrect: {result}")
            if result == "applied":
                await db.commit()
            return 0 if result == "applied" else 1

        if args.action == "anchor":
            if not args.name or not args.desc:
                print("--name and --desc required for anchor")
                return 2
            if not args.execute:
                print(f"DRY-RUN would upsert anchor: {args.name} | {args.desc}")
                return 0
            result = await upsert_anchor(db, user_id, args.name, args.desc)
            print(f"anchor: {result}")
            if result in ("created", "resurrected", "updated"):
                await db.commit()
            return 0 if result in ("created", "resurrected", "updated") else 1
    return 2


def main() -> int:
    p = argparse.ArgumentParser(description="W12 Dx-1 画像身份事实修复器（默认 dry-run）")
    p.add_argument("--user", required=True, help="用户名")
    p.add_argument("--execute", action="store_true", help="实际落库（默认 dry-run）")
    sub = p.add_subparsers(dest="action")
    sub.add_parser("list")
    r = sub.add_parser("resurrect")
    r.add_argument("concept_id")
    r.add_argument("--execute", action="store_true", help="实际落库（默认 dry-run）")
    s = sub.add_parser("supersede")
    s.add_argument("concept_id")
    s.add_argument("--set-desc", required=True)
    s.add_argument("--execute", action="store_true", help="实际落库（默认 dry-run）")
    a = sub.add_parser("anchor")
    a.add_argument("--name", required=True)
    a.add_argument("--desc", required=True)
    a.add_argument("--execute", action="store_true", help="实际落库（默认 dry-run）")
    args = p.parse_args()
    if not args.action:
        args.action = "list"
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
