"""本地设备注册表修复（W37，2026-11-05）。

背景：本地 `device_exec.json`（配对绑定文件）存在，但本地 DB
`client_executor_devices` 行丢失/被重建——`POST /api/conversations` 的
`execution_target=device:<self>` 会被 `assert_device_usable` 拒绝
（400 设备不存在或已解绑），新建对话曾因此完全失效。

本脚本从绑定文件重建本地注册行（token 只算 sha256，不回显）：
- 默认 dry-run；`--execute` 才写库。
- 同 device_id 已存在：更新 user/name/token_hash，`revoked=false`；
  若属主不同，默认拒绝（防劫持），显式 `--reassign` 才改。
- 只修本地 DB；云端注册表由配对流程维护，不在本脚本范围。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from datetime import datetime

from sqlalchemy import select

from app.db.database import AsyncSessionLocal, ClientExecutorDevice
from app.services.device_pairing_service import load_binding_record


def _dt(value):
    if not value:
        return datetime.utcnow()
    try:
        raw = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(raw).replace(tzinfo=None)
    except ValueError:
        return datetime.utcnow()


async def run(execute: bool, reassign: bool) -> int:
    rec = load_binding_record() or {}
    device_id = str(rec.get("device_id") or "").strip()
    token = str(rec.get("token") or "")
    user_id = str(rec.get("local_user_id") or "").strip()
    if not device_id or not token or not user_id:
        print("[repair_local_device] binding incomplete "
              f"(device_id={bool(device_id)} token={bool(token)} local_user_id={bool(user_id)})")
        return 1
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    name = str(rec.get("name") or "Logan's Mac")[:255]
    platform = str(rec.get("platform") or "")[:40] or None
    created_at = _dt(rec.get("created_at"))
    print(f"[repair_local_device] binding device={device_id} local_user={user_id} name={name}")
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(ClientExecutorDevice).where(
                ClientExecutorDevice.device_id == device_id)
        )).scalar_one_or_none()
        if row is not None:
            print(f"  existing row id={row.id} user={row.user_id} revoked={row.revoked}")
            if row.user_id != user_id and not reassign:
                print("  REFUSE: device_id belongs to a different user "
                      "(rerun with --reassign only after verifying ownership)")
                return 2
        if not execute:
            print("DRY-RUN: nothing written (rerun with --execute to apply)")
            return 0
        if row is None:
            row = ClientExecutorDevice(
                user_id=user_id,
                device_id=device_id,
                name=name,
                platform=platform,
                token_hash=token_hash,
                last_seen_at=datetime.utcnow(),
                revoked=False,
                created_at=created_at,
            )
            db.add(row)
        else:
            row.user_id = user_id
            row.name = name
            row.platform = platform
            row.token_hash = token_hash
            row.revoked = False
            row.last_seen_at = datetime.utcnow()
        await db.commit()
        await db.refresh(row)
        print(f"  OK row id={row.id} user={row.user_id} device={row.device_id} revoked={row.revoked}")
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--reassign", action="store_true")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(run(args.execute, args.reassign))
    except Exception as exc:  # noqa: BLE001
        print(f"[repair_local_device] failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
