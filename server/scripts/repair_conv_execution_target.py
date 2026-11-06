"""conv 执行目标 device rotation 修复（设备重配对换 id 后孤儿目标归位）。

背景：D-6 会话 execution_target 创建即定；设备「重新配对」按客户端现状会换
device_id（token 换新、旧设备行 revoked）——历史会话目标（device:旧id）立即孤儿化。
本脚本把指定用户下指向 from_device 的会话目标改指 to_device（同一物理机换身份，
非用户切换执行环境；旧值打印留档，可用 UPDATE 原样回滚）。

用法（dry-run 默认，--execute 才落库）：
    cd backend && python -m scripts.repair_conv_execution_target \
        --user-id <uid> --from <old_device_id> --to <new_device_id> [--execute]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db.database import AsyncSessionLocal, Conversation  # noqa: E402


async def _run(user_id: str, from_device: str, to_device: str, execute: bool) -> int:
    want_old = f"device:{from_device}"
    want_new = f"device:{to_device}"
    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(Conversation).where(
                        Conversation.user_id == user_id,
                        Conversation.execution_target == want_old,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            print(f"no conversations with execution_target={want_old} (user={user_id})")
            return 1
        for conv in rows:
            print(
                f"conv {conv.id} target {conv.execution_target!r} -> {want_new!r}"
                + ("" if execute else "  (dry-run)")
            )
        if execute:
            for conv in rows:
                conv.execution_target = want_new
            await db.commit()
            print(f"committed {len(rows)} row(s); 回滚=UPDATE conversations "
                  f"SET execution_target={want_old!r} WHERE id IN (...)")
        return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--user-id", required=True)
    p.add_argument("--from", dest="from_device", required=True)
    p.add_argument("--to", dest="to_device", required=True)
    p.add_argument("--execute", action="store_true")
    a = p.parse_args()
    return asyncio.run(_run(a.user_id, a.from_device, a.to_device, a.execute))


if __name__ == "__main__":
    raise SystemExit(main())
