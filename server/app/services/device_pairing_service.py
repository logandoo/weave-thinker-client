"""设备配对/凭据存储（PLAN_device_executor §1.2，主方案 §8.1）。

长效 device token 仅存本地 `$LOCALSTACK_HOME/app/device_exec.json`（0600，仿
localstack/gen_config.py·import_config.py 密钥落盘模式）；云端只存 token_hash。
token 不进日志。配对换发走 register_with_cloud（POST /api/devices，TLS）。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]  # …/server/app/services/ → 仓根（同 gen_config）


def localstack_home() -> Path:
    """LOCALSTACK_HOME 解析与 desktop/local-stack.cjs 优先级一致。"""
    for key in ("WEAVE_LOCALSTACK_HOME", "LOCALSTACK_HOME"):
        val = os.environ.get(key)
        if val:
            return Path(val).expanduser()
    return ROOT / ".localstack"


def store_path() -> Path:
    return localstack_home() / "app" / "device_exec.json"


def load_binding() -> Optional[Dict[str, Any]]:
    """读取配对记录；不存在/损坏 → None（fail-closed：无凭据不出站连接）。"""
    path = store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("token"):
        return None
    return data


def load_binding_record() -> Optional[Dict[str, Any]]:
    """原始记录读取（含 reset 墓碑/无 token 形态）——仅用于 device_id/身份连续性，
    凭据判断一律走 load_binding()（token 必备）。"""
    path = store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_binding(record: Dict[str, Any]) -> Path:
    """0600 落盘（文件与目录均不进日志内容）。"""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # 创建即 0600（评审 B#8：先写后 chmod 有世界可读窗口）
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))
    finally:
        os.close(fd)
    tmp.replace(path)
    os.chmod(path, 0o600)
    logger.info("device_exec binding saved (device_id=%s)", record.get("device_id", ""))
    return path


def reset_binding() -> bool:
    """清除本地凭据 token（撤销权在云端；本地 reset=断连+拒绝新调用）。

    墓碑语义（P0#3）：device_id/身份键/cloud_url 留存——重配对复用旧 device_id，
    历史会话 execution_target 不因 reset+重配对孤儿化。凭据面（load_binding）
    对无 token 记录仍 fail-closed 返回 None。"""
    record = load_binding_record()
    if not record or not record.get("token"):
        return False
    record.pop("token", None)
    save_binding(record)
    logger.info("device_exec credential cleared (tombstone kept: device_id=%s)",
                record.get("device_id", ""))
    return True


async def register_with_cloud(
    cloud_url: str,
    pairing_code: str,
    *,
    name: str = "",
    platform: str = "",
    device_id: str = "",
    tls_verify: bool = True,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """凭一次性短 TTL 配对码向云端注册，换长效 token（W16b 契约=上游已落地
    `POST /api/devices/pair`，body `{code, name?, platform?, device_id?}` →
    `{device_id, token, name?}`，client_devices.py:46-56/112）。

    token 仅此一次明文出现在响应中——不打印、不落日志。错码/过期码由云端
    拒绝（4xx），此处原样上抛供面板展示。
    """
    import httpx

    base = cloud_url.rstrip("/")
    payload = {
        "code": pairing_code,
        "name": name,
        "platform": platform,
    }
    # device_id 仅非空时发（重配对保代：历史会话 execution_target 连续性；
    # 上游同 id 重注册=同行换 token 保身份——见 upstream.toml handoff）
    if device_id:
        payload["device_id"] = device_id
    async with httpx.AsyncClient(verify=tls_verify, timeout=timeout) as client:
        resp = await client.post(
            f"{base}/api/devices/pair",
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, dict) or not data.get("token"):
        raise ValueError("云端响应缺少 device token")
    return data


async def push_rename_to_cloud(
    cloud_url: str,
    device_id: str,
    name: str,
    *,
    token: str,
    tls_verify: bool = True,
    timeout: float = 5.0,
) -> Dict[str, Any]:
    """改名推送云端（W25 handoff ③：`POST /api/devices/{device_id}/rename`，
    凭 device token，body `{name}`）。

    不抛异常：旧版云端无端点（404）/不可达均降级为 {"ok": False, "detail": ...}，
    本地改名不回滚、不虚构云端成功（W22「不虚构云端调用」契约延续）。"""
    import httpx

    base = cloud_url.rstrip("/")
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(device_id or "")):
        return {"ok": False, "detail": "设备标识非法，未推送云端；本地名称已保存"}
    try:
        async with httpx.AsyncClient(verify=tls_verify, timeout=timeout) as client:
            resp = await client.post(
                f"{base}/api/devices/{device_id}/rename",
                json={"name": name},
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:  # noqa: BLE001 — 网络失败=如实降级
        return {"ok": False, "detail": f"云端不可达（{type(exc).__name__}）；本地名称已保存，重新配对时会带上新名称"}
    if resp.status_code in (200, 201, 204):
        # 评审 I1：200 空体/HTML 伪应答不得虚报成功（SPA fallback/改写网关场景）——
        # 锚定响应体 renamed 严格真值（register_with_cloud 锚 token 同款防伪）。
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, dict) and data.get("renamed") is True:
            return {"ok": True, "detail": "云端已同步"}
        return {"ok": False, "detail": "云端应答异常（2xx 但未确认改名）；本地名称已保存"}
    if resp.status_code == 404:
        return {"ok": False, "detail": "云端暂不支持改名（旧版无改名端点）；本地名称已保存，重新配对时会带上新名称"}
    if resp.status_code in (401, 403):
        return {"ok": False, "detail": f"云端凭据已失效（HTTP {resp.status_code}）；本地名称已保存，请重新配对后云端名称随配对同步"}
    return {"ok": False, "detail": f"云端改名被拒（HTTP {resp.status_code}）；本地名称已保存"}
