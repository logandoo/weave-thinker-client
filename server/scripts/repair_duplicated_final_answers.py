"""存量脏消息幂等修复器 —— 输出单元契约历史欠账清偿（2026-10-14）。

两类病形（契约修复只防新增，本工具清历史行）：
  NARR 过程旁白粘连（「我先看看…」开头并入终稿，本地 conv d6ba1860 形态）——
       首句为严格过程旁白模式（动词白名单+短句）时剥除；剥后残留的
       `<!-- segment_split -->` 头标一并清掉。自动修复（形态已验证）。
  DUP  双答并存（「两遍几乎一致」，60 conv 90723ff0 形态）——**仅严格
       重起对**（后稿开头≈前稿开头）自动切；宽匹配（主题复述/引文重复/
       填充文本）一律只进 manual-review 报告，绝不自动删（防误删+保幂等）。
       人工确认后用 --message-id ID --cut-at N 精确手术。

幂等：NARR 修复后形态消失；严格 DUP 切后重起对消失。dry-run 默认。
用法：cd backend && python -m scripts.repair_duplicated_final_answers
        [--execute] [--message-id ID [--cut-at N]]
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_NARR_OPENERS = re.compile(
    r"^(我先看看|我先查|我先检索|让我先看|让我先查|让我先检索|我来查|我来看看|先看看)"
    r"[^。！？\n]{0,40}[。！？]"
)
_SEGMENT_MARK = "<!-- segment_split -->"


def _normalize(s: str) -> str:
    return re.sub(r"[\s，。、；：？！,.;:?!\"'“”‘’（）()【】\[\]—-]", "", s)


def detect_and_fix(content: str) -> tuple[str, str, str]:
    """返回 (fixed_content, kind, note)；kind ∈ {none, NARR, DUP, REVIEW}。"""
    if not content:
        return content, "none", ""

    # --- NARR：首句严格过程旁白粘连（动词白名单+短句+余量充足） ---
    # I7（A4.9）：完成体「了」句（「我先查了一下你的配置，发现三处问题」）是
    # 信息性首句不是过程旁白——含「了」不剥。
    m = _NARR_OPENERS.match(content)
    if m and 3 <= m.end() <= 48 and len(content) > m.end() + 60 and "了" not in content[:m.end()]:
        rest = content[m.end():].lstrip()
        # 剥离多段消息的孤悬分段头标（旁白本是独立段）
        while rest.startswith(_SEGMENT_MARK):
            rest = rest[len(_SEGMENT_MARK):].lstrip()
        return rest, "NARR", f"stripped opener: {content[:m.end()]!r}"

    # --- DUP：严格重起对才自动切；宽匹配只报告 ---
    n = len(content)
    if n >= 400:
        half = n // 2
        # 窗口不重叠（重叠区自匹配=假信号，曾致全库 match 400 误报）
        sm = difflib.SequenceMatcher(None, content[:half], content[half:], autojunk=False)
        best = max(sm.get_matching_blocks(), key=lambda b: b.size, default=None)
        if best and best.size >= 120:
            b_start = best.b + half
            cut = content.rfind("\n\n", 0, b_start)
            cut = cut + 2 if cut != -1 else b_start
            head, tail = content[:cut].rstrip(), content[cut:].strip()
            if tail:
                # 严格重起对：后稿开头与前稿开头近乎同文（重答复述型），
                # 且保留稿带答案结构标记（# 标题/** 强调）——填充/压测文本
                # （无结构）只报告，防连环切破坏幂等。
                restart = difflib.SequenceMatcher(
                    None, _normalize(head[:40]), _normalize(tail[:40])
                ).ratio() >= 0.55
                structured = ("#" in tail[:2000]) or ("**" in tail[:2000])
                # I5（A4.9）：覆盖率门——重起对的重叠块须覆盖两半的实质部分；
                # 「引用块开头+中后重现」的合法单稿（块长 131/半 397）被拦。
                coverage = best.size >= 0.5 * min(len(head), len(tail))
                if restart and structured and coverage:
                    return tail, "DUP", f"restart-dup cut at {cut} (match {best.size})"
                # 宽命中：主题复述/引文/填充 —— 只报告
                return content, "REVIEW", (
                    f"overlap match {best.size} at tail~{b_start} 未达重起阈值"
                    f"（head/tail 开头相似度 {difflib.SequenceMatcher(None, _normalize(head[:40]), _normalize(tail[:40])).ratio():.2f}）"
                    "——人工确认后用 --cut-at 手术"
                )
    return content, "none", ""


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="落地修复（默认 dry-run）")
    ap.add_argument("--message-id", default="", help="只处理指定消息")
    ap.add_argument("--cut-at", type=int, default=-1, help="人工手术：从该字符偏移起保留后稿")
    args = ap.parse_args()

    from sqlalchemy import select
    from app.db.database import AsyncSessionLocal, Message

    findings: list[dict] = []
    backups: list[dict] = []
    scanned = 0
    async with AsyncSessionLocal() as db:
        q = select(Message).where(Message.role == "assistant")
        if args.message_id:
            q = q.where(Message.id == args.message_id)
        rows = (await db.execute(q)).scalars().all()
        scanned = len(rows)
        for row in rows:
            content = row.content or ""
            if args.cut_at >= 0:
                # I6（A4.9）：手术口防护——必须与 --message-id 同用、偏移越界拒绝、
                # 余量下限；不满足即跳过该行（绝不落空串）。
                if not args.message_id:
                    print("ERROR: --cut-at 需与 --message-id 同用", file=sys.stderr)
                    return 2
                if not (0 < args.cut_at < len(content) - 60):
                    findings.append({"id": str(row.id), "kind": "REJECTED",
                                     "note": f"cut_at {args.cut_at} 越界（len={len(content)}）"})
                    continue
                fixed = content[args.cut_at:].strip()
                kind = "DUP-MANUAL"
                note = f"manual cut at {args.cut_at}"
            else:
                fixed, kind, note = detect_and_fix(content)
            if kind in ("none",):
                continue
            findings.append({
                "id": str(row.id),
                "conversation_id": str(row.conversation_id),
                "kind": kind,
                "before_len": len(content),
                "after_len": len(fixed),
                "note": note,
            })
            if args.execute and kind in ("NARR", "DUP", "DUP-MANUAL"):
                # I7（A4.9）：破坏性写前逐行备份（before/after JSONL）——
                # 多租户库无备份不落刀。
                backups.append({"id": str(row.id), "kind": kind,
                                "before": content, "after": fixed})
                row.content = fixed
        if args.execute and findings:
            await db.commit()
        if args.execute and backups:
            bp = Path(__file__).resolve().parent.parent / "output_files" / (
                "repair_backup_" + __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S") + ".jsonl")
            bp.parent.mkdir(parents=True, exist_ok=True)
            with open(bp, "w", encoding="utf-8") as bf:
                for b in backups:
                    bf.write(json.dumps(b, ensure_ascii=False) + "\n")
            print(f"backup written: {bp}", file=sys.stderr)

    print(json.dumps({
        "mode": "execute" if args.execute else "dry-run",
        "scanned": scanned,
        "count": len(findings),
        "findings": findings,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
