#!/usr/bin/env python3
"""ASR 热词表 target_model 重同步（一次性运维脚本，2026-09-29 ASR 迁移波）。

背景：DashScope 热词表在 create_vocabulary 时绑定 target_model；识别模型切换
（fun-asr-realtime → qwen-audio-3.1-asr-flash-streaming）后旧表仍绑旧模型，
热词**静默失效**。本脚本逐用户按当前 `[endpoints.asr]` 模型重建热词表并回写
`user_asr_hotwords.dashscope_vocabulary_id`，随后删除旧表。

用法（backend/ 下）：
    python -m scripts.resync_asr_vocabulary            # dry-run（只读，列计划）
    python -m scripts.resync_asr_vocabulary --execute  # 真实重建（账号级节流 sleep 5s）
"""
import argparse
import asyncio
import sys

from sqlalchemy import select

sys.path.insert(0, ".")

from app.db.database import AsyncSessionLocal, UserAsrHotword  # noqa: E402
from app.services.asr_vocabulary_service import ASRVocabularyService  # noqa: E402
from app.services.http_client import get_shared_async_client  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="真实执行（默认 dry-run）")
    args = ap.parse_args()

    svc = ASRVocabularyService()
    print(f"[plan] target_model = {svc._target_model}  prefix = {svc._prefix}")
    if not svc.enabled:
        print("[plan] ASR API key 未配置，无法执行")
        return 1

    execute = args.execute
    print(f"[plan] mode = {'EXECUTE' if execute else 'DRY-RUN'}")
    client = get_shared_async_client()

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(UserAsrHotword))).scalars().all()
        by_user: dict[str, list] = {}
        for r in rows:
            by_user.setdefault(r.user_id, []).append(r)
        print(f"[plan] {len(by_user)} 个用户共 {len(rows)} 条热词待重同步")

        for user_id, items in by_user.items():
            try:
                old_ids = sorted({r.dashscope_vocabulary_id for r in items if r.dashscope_vocabulary_id})
                vocabulary = [
                    {"text": r.text, "weight": 50 if int(r.weight or 4) == 50 else max(1, min(5, int(r.weight or 4)))}
                    | ({"lang": r.lang} if r.lang else {})
                    for r in items
                    if r.text and r.text.strip()
                ]
                if not vocabulary:
                    print(f"[user {user_id}] 无有效热词行，跳过")
                    continue
                print(f"[user {user_id}] words={len(vocabulary)} old_vocab={old_ids or 'None'}")
                if not execute:
                    continue
                new_id = await svc._create(client, vocabulary)
                if not new_id:
                    print(f"[user {user_id}] !! create_vocabulary 失败，跳过（旧行未动）")
                    await asyncio.sleep(5)  # 失败路径同样节流（限流场景勿锤）
                    continue
                for r in items:
                    r.dashscope_vocabulary_id = new_id
                await db.commit()
                print(f"[user {user_id}] OK new_vocab={new_id}（旧行已回写）")
                for oid in old_ids:
                    if oid != new_id:
                        ok = await svc._delete(client, oid)
                        print(f"[user {user_id}] {'deleted old' if ok else '!! delete FAILED (leak)'} {oid}")
                await asyncio.sleep(5)  # 账号级节流
            except Exception as exc:  # 单用户失败不拖垮批次（A4.9 R2-I3/R3-M3）
                try:
                    await db.rollback()  # 清 PendingRollbackError，保后续用户可用
                except Exception:
                    pass
                print(f"[user {user_id}] !! 异常中止该用户: {exc!r}（其后用户继续；如已 create 未回写，重跑会新建并可手工清孤儿）")
                await asyncio.sleep(5)

    print("[done]" if execute else "[dry-run done] 加 --execute 执行")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
