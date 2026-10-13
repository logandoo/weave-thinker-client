"""workspace 文件同步端点（2026-09-29 波；前缀 /api/sync/workspace，全部 JWT）。

- ``GET /status?path=<rel>``：预览前状态——本地是否已有；缺失且已绑定云端时
  探测远端是否存在/大小（本地已有则跳过远端探测）。前端据此决定直接加载、
  小文件自动拉取或「当前文件较大，是否同步？」。
- ``POST /fetch`` body ``{"path": "<rel>"}``：按需单文件同步（大文件预览的
  「同步」按钮入口；上限 [sync] workspace_max_mb）。

全量同步（resync）时的小文件自动拉取由 SyncClient 周期内完成，不在此端点。
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.config import get_config
from app.core.deps import get_current_user
from app.db.database import User
from app.services.sync_client import sync_client
from app.services.workspace_sync_service import normalize_workspace_path

router = APIRouter(prefix="/api/sync/workspace", tags=["sync"])

_FETCH_STATUS = {
    "not_bound": 400,
    "invalid_path": 400,
    "remote_missing": 404,
    "too_large": 413,
    "remote_error": 502,
    "network": 502,
    "local_error": 500,
}

_FETCH_MESSAGE = {
    "not_bound": "未绑定云端账号",
    "invalid_path": "非法文件路径",
    "remote_missing": "云端不存在该文件",
    "too_large": "文件超过按需同步上限",
    "remote_error": "云端下载失败",
    "network": "云端不可达",
    "local_error": "本地写入失败",
}


def _sync_enabled() -> None:
    cfg = get_config()
    if not cfg.sync_enabled or not cfg.sync_workspace_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


@router.get("/status")
async def workspace_file_status(
    path: str = Query(..., description="workspace-relative file path"),
    current_user: User = Depends(get_current_user),
):
    _sync_enabled()
    if len(path) > 1024:
        raise HTTPException(status_code=400, detail="path too long")
    # 契约（判据 2）：无法归一的绝对路径/非法形 → 400 invalid，而非 200 静默
    if normalize_workspace_path(path) is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "workspace_sync_invalid_path", "message": "非法文件路径"},
        )
    return await sync_client.workspace_file_status(path, current_user.id)


@router.post("/fetch")
async def workspace_file_fetch(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    _sync_enabled()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — 非法 JSON 统一 400
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    raw_path = body.get("path")
    path = raw_path if isinstance(raw_path, str) else ""
    if not path or len(path) > 1024:
        raise HTTPException(status_code=400, detail="path required")
    result = await sync_client.fetch_workspace_file(path, current_user.id)
    if result.get("ok"):
        return result
    reason = str(result.get("reason") or "remote_error")
    raise HTTPException(
        status_code=_FETCH_STATUS.get(reason, 502),
        detail={
            "code": f"workspace_sync_{reason}",
            "message": _FETCH_MESSAGE.get(reason, "同步失败"),
        },
    )
