"""本地「设备执行」面板 API（PLAN_device_executor §1.2；契约=server/API.md「设备执行器」节）。

P0 只做本地 sidecar 配置动作：状态只读、配对码换 token（sidecar 凭码调云端
POST /api/devices，TLS）、reset 清本地凭据。设备列表/撤销管理 UI 在云端 web；
本渲染器不直接调云端 API（跨源缺口不扩大）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.deps import get_current_user
from app.core.config import get_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/client-exec", tags=["client-exec"])


class PairRequest(BaseModel):
    # 缺配对码走 400（API.md 契约；不用 FastAPI 422 校验家族——面板统一 4xx 语义）
    pairing_code: str = ""
    cloud_url: str = ""
    name: str = ""
    platform: str = ""
    device_id: str = ""  # 重配对保代（P0#2）：复用旧绑定 device_id，缺省由后端回落
    tls_skip: bool = False


_IMPL_MARKER = "W20-device-identity"


def _build_id() -> str:
    """构建世代（P1#4）：env WEAVE_BUILD_ID → 打包期写入 resource_root/BUILD_ID → dev。"""
    import os as _os

    env = _os.environ.get("WEAVE_BUILD_ID", "").strip()
    if env:
        return env[:80]
    try:
        from app.core.config import get_config

        p = get_config().resource_root / "BUILD_ID"
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()[:80] or "dev"
    except Exception:
        logger.debug("BUILD_ID read failed", exc_info=True)
    return "dev"


async def _name_gate_ready() -> bool:
    """名门期望值是否可算：配对记录云端身份在档，或本机 sync 绑定已回填 cloud_user_id。"""
    from app.db.database import AsyncSessionLocal, SyncState
    from app.services.device_pairing_service import load_binding

    binding = load_binding() or {}
    if binding.get("cloud_username") and binding.get("cloud_user_id"):
        return True
    local_user_id = str(binding.get("local_user_id") or "")
    if not local_user_id:
        return False
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(SyncState, local_user_id)
            return bool(row is not None and row.cloud_user_id and row.cloud_username)
    except Exception:
        return False


@router.get("/status")
async def status(current_user=Depends(get_current_user)):
    from app.services.device_executor_service import device_executor

    out = device_executor.status()
    out["name_gate_ready"] = await _name_gate_ready()
    out["build_id"] = _build_id()
    out["impl_marker"] = _IMPL_MARKER
    # P2#6：绑定云端身份 + 名门期望值（面板现场诊断免翻文件；无凭据字段）
    from app.services.device_executor_service import expected_workspace_ctx
    from app.services.device_pairing_service import load_binding_record

    rec = load_binding_record() or {}
    c_name = str(rec.get("cloud_username") or "")
    c_uid = str(rec.get("cloud_user_id") or "")
    out["cloud_username"] = c_name
    out["cloud_user_id"] = c_uid
    out["workspace_ctx_expected"] = expected_workspace_ctx(c_name, c_uid) or ""
    return out


@router.post("/pair")
async def pair(req: PairRequest, current_user=Depends(get_current_user)):
    import httpx

    from app.services import device_pairing_service as pairing
    from app.services.device_executor_service import device_executor

    if not (req.pairing_code or "").strip():
        raise HTTPException(status_code=400, detail="pairing_code 缺失")
    cloud_url = await _resolve_cloud_url_async(req.cloud_url)
    if cloud_url and not cloud_url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="cloud_url 仅允许 http(s)")
    if not cloud_url:
        raise HTTPException(status_code=400, detail="cloud_url 缺失（提供 cloud_url 或先绑定同步服务器）")

    # device_id 连续性（P0#1-#3）：req → 旧绑定（含 reset 墓碑）→ 空（首配）。
    # req=="new"=逃生口（评审 r2-F3）：强制新代（不复用墓碑，异属主/格式冲突解死局）。
    prev_rec = pairing.load_binding_record() or {}
    req_id = (req.device_id or "").strip()
    if req_id == "new":
        effective_device_id = ""
    else:
        effective_device_id = req_id or str(prev_rec.get("device_id") or "")
        # 仅同云复用（评审 r1-F5：换云不携带旧云 device_id）
        if not req_id and prev_rec.get("cloud_url") and prev_rec.get("cloud_url") != cloud_url:
            effective_device_id = ""

    data = None
    device_id_changed = False
    # name 解析链（acceptance #24）：req.name → 旧绑定名（重配对空名不丢显示名）→ platform 缺省
    effective_name = (req.name or "").strip() or str(prev_rec.get("name") or "") or f"{req.platform or 'device'}"
    try:
        data = await pairing.register_with_cloud(
            cloud_url,
            req.pairing_code,
            name=effective_name,
            platform=req.platform,
            device_id=effective_device_id,
            tls_verify=not req.tls_skip,
        )
    except httpx.HTTPStatusError as exc:
        # 评审 r1-F1/r2-F1 降级兜底：携 device_id 撞上游 unique 约束（现上游 500）等
        # 注册期失败 → 去 device_id 重试一次（恢复旧可用性：配对成功但换代），
        # 错码/过期（4xx 且非注册冲突）不重试、原样透传。
        if effective_device_id and exc.response.status_code >= 500:
            logger.warning("pair with device_id failed (%s) — retrying without (generation change)",
                           exc.response.status_code)
            try:
                data = await pairing.register_with_cloud(
                    cloud_url,
                    req.pairing_code,
                    name=effective_name,
                    platform=req.platform,
                    device_id="",
                    tls_verify=not req.tls_skip,
                )
            except httpx.HTTPStatusError as exc2:
                raise HTTPException(
                    status_code=exc2.response.status_code,
                    detail=f"云端拒绝配对: {exc2.response.text[:200]}",
                )
            except ValueError as exc2:
                raise HTTPException(status_code=400, detail=str(exc2))
            except httpx.HTTPError as exc2:
                raise HTTPException(status_code=502, detail=f"云端不可达: {exc2}")
            effective_device_id = ""
            device_id_changed = True
        else:
            # 4xx/5xx 透传云端状态（错码/过期码原样呈现）
            raise HTTPException(
                status_code=exc.response.status_code,
                detail=f"云端拒绝配对: {exc.response.text[:200]}",
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"云端不可达: {exc}")

    # 评审 r1-F3/r2-F4：wire 真相 vs 连续性意图——响应 id 与请求不一致必须可见
    if effective_device_id and str(data.get("device_id") or "") != effective_device_id:
        logger.warning("pair device_id changed by cloud: requested=%s got=%s",
                       effective_device_id, data.get("device_id"))
        device_id_changed = True

    # 换代标注真相（re-review NEW-2 修复轮）：换云/"new" 逃生口不带 id 时上分支不触发，
    # 但 wire 实际换代——凡旧记录 device_id 与本次不等即标注（前端据实提示，不假报「保持连续」）。
    prev_id = str(prev_rec.get("device_id") or "")
    if prev_id and str(data.get("device_id") or "") != prev_id:
        device_id_changed = True

    token = str(data.get("token") or "")
    if not token:
        raise HTTPException(status_code=400, detail="云端响应缺少 device token")

    record = {
        "cloud_url": cloud_url,
        "device_id": str(data.get("device_id") or effective_device_id or ""),
        "name": str(data.get("name") or effective_name or ""),
        "platform": str(data.get("platform") or req.platform or ""),
        "token": token,
        "tls_verify": not req.tls_skip,
        "created_at": datetime.now(timezone.utc).isoformat(),
        # W20 身份绑定（PLAN_device_workspace_identity_impl T2）：
        # local_user_id=配对发起者（显式属主，禁「首用户」推断）；
        # cloud_user_id/cloud_username=pair 响应携带云端身份时持久化（名门期望值来源，
        # 上游 W20 handoff 字段；旧响应缺省=空串，名门侧回落 sync 绑定或 fail-closed）。
        "local_user_id": str(getattr(current_user, "id", "") or ""),
        # 评审 r1-F4：响应缺云端身份字段时回落墓碑旧值（重配对不清身份）
        "cloud_user_id": str(data.get("user_id") or prev_rec.get("cloud_user_id") or ""),
        "cloud_username": str(data.get("username") or prev_rec.get("cloud_username") or ""),
    }
    pairing.save_binding(record)
    device_executor.wake()
    logger.info("device paired: %s @ %s", record["device_id"], cloud_url)
    return {
        "device_id": record["device_id"],
        "name": record["name"],
        "platform": record["platform"],
        "cloud_url": record["cloud_url"],
        "device_id_changed": device_id_changed,
    }


@router.post("/reset")
async def reset(current_user=Depends(get_current_user)):
    from app.services import device_pairing_service as pairing
    from app.services.device_executor_service import device_executor

    cleared = pairing.reset_binding()
    device_executor.wake()
    return {"reset": bool(cleared)}


class RenameRequest(BaseModel):
    name: str = ""


@router.post("/rename")
async def rename(req: RenameRequest, current_user=Depends(get_current_user)):
    """改设备显示名（本地绑定记录 name，acceptance #22/#25）。

    语义=修改（不动 token/device_id）；重置/替换绑定走 reset/pair，三动作分离。
    W25：本地写后推送云端改名（handoff ③ 端点形状）；旧版云端 404/不可达降级
    renamed_cloud=false+cloud_note 如实回执，本地改名不回滚。"""
    from app.services import device_pairing_service as pairing

    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    if len(name) > 128:
        raise HTTPException(status_code=400, detail="name 超长（>128）")
    record = pairing.load_binding_record()
    if not record or not record.get("token"):
        raise HTTPException(status_code=400, detail="未配对，无法修改设备名称")
    record["name"] = name
    pairing.save_binding(record)
    logger.info("device renamed (local): device_id=%s", record.get("device_id", ""))
    # W25：改名推送云端（handoff ③ 端点形状）——旧版云端/不可达如实降级，本地不回滚。
    renamed_cloud = False
    cloud_note = ""
    cloud_url = str(record.get("cloud_url") or "")
    dev_token = str(record.get("token") or "")
    if not cloud_url:
        cloud_note = "未配置云端（本地名称已保存）"
    if cloud_url and dev_token:
        res = await pairing.push_rename_to_cloud(
            cloud_url, str(record.get("device_id") or ""), name,
            token=dev_token, tls_verify=bool(record.get("tls_verify", True)),
        )
        renamed_cloud = bool(res.get("ok"))
        cloud_note = str(res.get("detail") or "")
    return {
        "device_id": str(record.get("device_id") or ""), "name": name,
        "renamed": True, "renamed_cloud": renamed_cloud, "cloud_note": cloud_note,
    }


async def _resolve_cloud_url_async(requested: str) -> str:
    url = (requested or "").strip().rstrip("/")
    if url:
        return url
    config = get_config()
    if config.client_exec_cloud_url:
        return config.client_exec_cloud_url
    try:
        from sqlalchemy import select

        from app.db.database import AsyncSessionLocal, SyncState

        async with AsyncSessionLocal() as db:
            res = await db.execute(select(SyncState).limit(1))
            state = res.scalar_one_or_none()
            return (getattr(state, "server_url", "") or "").rstrip("/") or ""
    except Exception:
        return ""


# ---- W32 执行环境云端联动：云端执行器注册表代理（契约=server/API.md）----
# 云端注册表是「其他绑定的客户端」的唯一事实源（本地 /devices 是本机栈自己的注册表，
# 客户端场景为空）；本机设备（device_exec.json 绑定）带 is_self 呈现「本地」语义。

def _self_device_id() -> str:
    try:
        from app.services.device_pairing_service import load_binding

        return str((load_binding() or {}).get("device_id") or "")
    except Exception:
        return ""


@router.get("/cloud-devices")
async def cloud_devices(current_user=Depends(get_current_user)):
    from app.services.cloud_link_service import (
        LinkAuthError,
        LinkUnavailable,
        cloud_link_service,
    )

    user_id = str(current_user.id)
    if not await cloud_link_service.is_linked(user_id):
        return {"linked": False, "devices": []}
    try:
        resp = await cloud_link_service.request(user_id, "GET", "/api/devices")
    except LinkUnavailable:
        raise HTTPException(status_code=500, detail="云端不可达：无法获取设备列表，请检查网络后重试")
    except LinkAuthError:
        raise HTTPException(status_code=400, detail="云端凭据失效：请在「云端关联」重新绑定后重试")
    if resp.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail=f"云端设备列表获取失败（HTTP {resp.status_code}）",
        )
    try:
        payload = resp.json()
    except Exception:
        raise HTTPException(status_code=500, detail="云端设备列表响应异常（非 JSON）")
    rows = payload.get("devices") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        rows = []
    self_id = _self_device_id()
    devices = [
        {
            "device_id": str(r.get("device_id") or ""),
            "name": r.get("name") or "",
            "platform": r.get("platform") or "",
            "online": bool(r.get("online")),
            "revoked": bool(r.get("revoked")),
            "last_seen_at": r.get("last_seen_at"),
            "is_self": bool(self_id and str(r.get("device_id") or "") == self_id),
        }
        for r in rows
        if isinstance(r, dict) and r.get("device_id")
    ]
    return {"linked": True, "devices": devices}


@router.post("/cloud-devices/{device_id}/revoke")
async def cloud_device_revoke(device_id: str, current_user=Depends(get_current_user)):
    from app.services.cloud_link_service import (
        LinkAuthError,
        LinkUnavailable,
        cloud_link_service,
    )

    if _self_device_id() and device_id == _self_device_id():
        raise HTTPException(
            status_code=400,
            detail="不能撤销本机设备：请走「云端关联」重置本地凭据，或在云端网页端管理其他设备",
        )
    user_id = str(current_user.id)
    try:
        resp = await cloud_link_service.request(
            user_id, "POST", f"/api/devices/{device_id}/revoke"
        )
    except LinkUnavailable:
        raise HTTPException(status_code=500, detail="云端不可达：撤销未生效，请稍后重试")
    except LinkAuthError:
        raise HTTPException(status_code=400, detail="云端凭据失效：请在「云端关联」重新绑定后重试")
    if resp.status_code // 100 != 2:
        detail = ""
        try:
            detail = str(resp.json().get("detail") or "")
        except Exception:
            detail = ""
        raise HTTPException(
            status_code=400,
            detail=detail or f"云端拒绝撤销（HTTP {resp.status_code}）",
        )
    return {"revoked": True}
