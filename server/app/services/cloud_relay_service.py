"""W32 执行环境云端联动 — 云端中继（方案 A 核心）。

客户端（本地栈）对「云端」（execution_target=None）与「其他绑定的客户端」
（device:<非本机>）会话的 turn 由**云端执行**：本模块把 /api/chat/stream* 与
/api/devices/{id}/fs/* 请求以云端账户身份代理到云端，SSE 帧逐块透传（真流式）。

路由谓词（Consistency Hub 单一真源）：
    relay ⇔ 云端链路在 且 (target 为空（云端）或 device:<非本机且不在本地注册表>)
本机目标（device:<self>）= 本地直执行短路（remote_dispatch_router 同判）；
本地注册表设备走本地派发；无云端链接=本地执行（W26 退化）；非法值不中继。

fail-closed（D-8 不放松）：云端不可达/凭据失效 → 明确 502/500 JSON 报错，
绝不静默改道本地执行、绝不假成功。

测试缝：``_local_device_ids`` · ``_resolve_user_and_target``（中间件用户/目标解析）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional, Set, Tuple

import httpx

from app.services.cloud_link_service import (
    LinkAuthError,
    LinkUnavailable,
    cloud_link_service,
)

logger = logging.getLogger(__name__)

__all__ = [
    "should_relay",
    "should_relay_fs",
    "proxy_to_cloud",
    "relay_asgi",
    "intercepted_path",
]

# 拦截面：chat 流面（含续流/状态/停止/插话）与设备文件代理面
_CHAT_PREFIXES = (
    "/api/chat/stream",
    "/api/chat/stream/resume",
    "/api/chat/stream/status/",
    "/api/chat/stream/stop/",
    "/api/chat/stream/interject/",
)


def intercepted_path(path: str) -> Optional[str]:
    """返回 'chat' | 'fs' | None（是否属中继拦截面）。"""
    if path == "/api/chat/stream" or any(
        path.startswith(p) for p in _CHAT_PREFIXES if p != "/api/chat/stream"
    ):
        return "chat"
    if path.startswith("/api/devices/") and "/fs/" in path:
        return "fs"
    return None


async def _local_device_ids(user_id: str) -> Optional[Set[str]]:
    """本地注册表 device_id 集合；读取失败返回 None=未知（fail-closed 不中继）。测试缝。"""
    from sqlalchemy import select

    from app.db.database import AsyncSessionLocal, ClientExecutorDevice

    try:
        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(ClientExecutorDevice.device_id).where(
                    ClientExecutorDevice.user_id == user_id,
                    ClientExecutorDevice.revoked == False,  # noqa: E712
                )
            )
            return {str(r[0]) for r in res.all()}
    except Exception:
        logger.warning("relay: local device registry read failed", exc_info=True)
        return None


def _self_device_id() -> Optional[str]:
    """本机设备 id；绑定读取失败返回 None=未知（fail-closed：不中继也不短路，
    由派发层给出明确错误——评审 A/B Important：未知不得 fail-open 中继改道）。"""
    try:
        from app.services.device_pairing_service import load_binding

        return str((load_binding() or {}).get("device_id") or "")
    except Exception:
        logger.warning("relay: self device id unreadable", exc_info=True)
        return None


async def should_relay(user_id: str, target: Optional[str]) -> bool:
    """中继路由谓词（Consistency Hub）。非法目标/本机/本地注册表/无链接 → False。"""
    if not await cloud_link_service.is_linked(user_id):
        return False
    if not target:
        return True  # 空目标 = 云端（API.md「null 表示恢复云端执行」）
    if not str(target).startswith("device:"):
        return False
    device_id = str(target).split(":", 1)[1].strip()
    if not device_id:
        return False  # 形如 device: 的畸形目标不中继（评审 Minor）
    self_id = _self_device_id()
    if self_id is None:
        return False  # 本机身份未知：fail-closed 不中继（派发层给明确错误）
    if device_id == self_id:
        return False  # 本机 = 本地直执行短路
    local_ids = await _local_device_ids(user_id)
    if local_ids is None:
        return False  # 注册表读取失败：fail-closed 不中继（评审 A/B Important）
    if device_id in local_ids:
        return False  # 本地注册表设备走本地派发
    return True


async def should_relay_fs(user_id: str, device_id: str) -> bool:
    """设备文件代理面：非本机且不在本地注册表 + 有云端链接 → 代理云端设备 fs。"""
    if not await cloud_link_service.is_linked(user_id):
        return False
    self_id = _self_device_id()
    if self_id is None:
        return False
    if device_id == self_id:
        return False
    local_ids = await _local_device_ids(user_id)
    if local_ids is None:
        return False
    return device_id not in local_ids


async def proxy_to_cloud(
    user_id: str,
    method: str,
    path: str,
    query: str = "",
    content: bytes = b"",
    headers: Optional[dict] = None,
    *,
    stream: bool = False,
    timeout: float = 180.0,
    ensure_conv_id: Optional[str] = None,
) -> httpx.Response:
    """云端代理（Bearer 注入 + 404 会话 ensure 重试一次）。stream=True 时调用方
    负责 aclose（aiter_bytes 泵完）。云端不可达 → LinkUnavailable（fail-closed）。"""
    url_path = path + (f"?{query}" if query else "")
    if ensure_conv_id is None and content and path.startswith("/api/chat/stream"):
        # 未显式给会话 id 时从请求体导出（/chat/stream 系均带 conversation_id）
        try:
            ensure_conv_id = (json.loads(content.decode("utf-8")) or {}).get("conversation_id")
        except Exception:
            ensure_conv_id = None
    fwd = {}
    for k, v in (headers or {}).items():
        if k.lower() in ("host", "content-length", "authorization", "connection",
                         "accept-encoding", "transfer-encoding", "cookie",
                         "keep-alive", "te", "trailer", "upgrade", "proxy-authorization"):
            continue
        if k.lower().startswith("x-forwarded-") or k.lower().startswith("proxy-"):
            continue
        fwd[k] = v

    async def _once() -> httpx.Response:
        return await cloud_link_service.request(
            user_id, method, url_path,
            content=content or None,
            extra_headers=fwd or None,
            timeout=timeout,
            stream=stream,
        )

    resp = await _once()
    if resp.status_code == 404 and ensure_conv_id:
        # 云端尚无该会话（本地新建未推上）：ensure（wake+有界轮询）后重试一次。
        # 评审 A/B Important：ensure 失败也必须释放首个流式响应（连接泄漏）。
        try:
            ok = await cloud_link_service.ensure_conversation(user_id, ensure_conv_id)
        except BaseException:
            if stream:
                await resp.aclose()
            raise
        if ok:
            if stream:
                await resp.aclose()
            resp = await _once()
    return resp


async def _resolve_user_and_target(
    scope: dict, body: bytes, kind: str
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """中间件解析：(user_id, conversation_target, conv_id)。未认证/解析失败 → (None, ...)。
    测试缝：monkeypatch 本函数。"""
    headers = {k.decode("latin1"): v.decode("latin1") for k, v in scope.get("headers", [])}
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None, None, None
    token = auth[7:]
    conv_id = None
    if kind == "chat":
        path = scope.get("path", "")
        if body:
            try:
                conv_id = (json.loads(body.decode("utf-8")) or {}).get("conversation_id")
            except Exception:
                conv_id = None
        if not conv_id:
            # /status/{id} /stop/{id} /interject/{id}：id 在路径尾
            tail = path.rstrip("/").rsplit("/", 1)[-1]
            if tail and tail not in ("stream", "resume", "status", "stop", "interject"):
                conv_id = tail
    else:
        # /api/devices/{device_id}/fs/... → device_id
        conv_id = None
    try:
        from sqlalchemy import select

        from app.services.auth_service import decode_access_token
        from app.db.database import AsyncSessionLocal, Conversation, User

        payload = decode_access_token(token)
        if not payload:
            return None, None, None
        user_id = str(payload.get("sub") or "")
        if not user_id:
            return None, None, None
        target = None
        async with AsyncSessionLocal() as db:
            if kind == "chat" and conv_id:
                res = await db.execute(
                    select(Conversation.execution_target).where(
                        Conversation.id == conv_id, Conversation.user_id == user_id
                    )
                )
                row = res.first()
                if row is None:
                    # 会话不在本地库（极端时序）：按云端目标中继由 ensure 兜底
                    target = None
                else:
                    target = row[0]
        return user_id, target, conv_id
    except Exception:
        logger.exception("relay: user/target resolution failed")
        return None, None, None


def _json_error(status: int, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail})


async def relay_asgi(scope, receive, send, downstream: Callable) -> None:
    """ASGI 中继中间件体：命中拦截面且谓词 True → 云端代理（SSE 逐块透传）；
    否则把已读 body 重放给下游 app 原样执行。"""
    if scope.get("type") != "http":
        await downstream(scope, receive, send)
        return
    path = scope.get("path", "")
    kind = intercepted_path(path)
    if kind is None:
        await downstream(scope, receive, send)
        return

    # 读全请求体（chat/stream 均为小 JSON；fs 面为 GET 无体）
    body = b""
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            break
        body += msg.get("body", b"") or b""
        if not msg.get("more_body"):
            break

    user_id, target, conv_id = await _resolve_user_and_target(scope, body, kind)

    relay_needed = False
    if user_id:
        if kind == "chat":
            relay_needed = await should_relay(user_id, target)
        else:
            device_id = path.split("/api/devices/", 1)[1].split("/", 1)[0]
            relay_needed = await should_relay_fs(user_id, device_id)

    if not relay_needed:
        # 未认证/无链接/本机/本地注册表 → 放行下游（含未认证：交给既有 401 链）。
        # 评审 B Critical：重放后必须继续委托**原 receive**——合成 http.disconnect
        # 会被 sse_starlette/StreamingResponse 的断连监听当作客户端离开而砍掉本地流。
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": False,
                }
            return await receive()

        await downstream(scope, replay_receive, send)
        return

    headers = {
        k.decode("latin1"): v.decode("latin1") for k, v in scope.get("headers", [])
    }
    method = scope.get("method", "GET")
    query = scope.get("query_string", b"").decode("latin1")
    try:
        resp = await proxy_to_cloud(
            user_id, method, path,
            query=query,
            content=body,
            headers=headers,
            stream=True,
            ensure_conv_id=conv_id if kind == "chat" else None,
        )
    except LinkAuthError as exc:
        resp = _json_error(400, f"云端凭据失效：{exc}")
    except LinkUnavailable as exc:
        resp = _json_error(502, f"云端不可达：{exc}（本次未在本地执行，不静默改道）")

    try:
        resp_headers = [
            (k.encode("latin1"), v.encode("latin1"))
            for k, v in resp.headers.items()
            if k.lower() not in ("content-length", "transfer-encoding", "connection",
                                 "content-encoding")
        ]
        await send({"type": "http.response.start", "status": resp.status_code,
                    "headers": resp_headers})
        # ASGI send 类型必须是 http.response.body（http.body 是 receive 类型——评审 B
        # Critical 实证：uvicorn 下该消息直接 RuntimeError）；尾帧只发一次。
        try:
            async for chunk in resp.aiter_bytes():
                if chunk:
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
        except httpx.HTTPError:
            # 流中断：已发帧保真，尾帧缺失交由前端既有断流恢复链
            logger.warning("relay: cloud stream interrupted", exc_info=True)
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    finally:
        try:
            await resp.aclose()
        except Exception:
            pass
