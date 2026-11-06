"""W32 执行环境云端联动 — 云端链路服务（中继凭据/令牌/请求面）。

客户端（本地栈）中继「云端/其他客户端」目标的 turn 时，需要以**云端账户身份**
调用云端 API。凭据源 = SyncState{server_url, cloud_username, cloud_password}
（本机同盒威胁模型 D-13，密码不出本模块）。

契约（Consistency Hub）：
- ``CloudLinkService.request(user_id, method, path, *, json=None, params=None,
  timeout=30.0, stream=False) -> httpx.Response``：自动登录取 JWT、请求带
  Bearer；401 重登一次重试（token 轮换自愈）；``path`` 以 ``/api`` 开头。
- ``is_linked(user_id) -> bool``：该用户是否有可用云端绑定。
- ``ensure_conversation(user_id, conv_id) -> bool``：确保云端存在该会话
  （本地新建的会话可能未随同步推上）：GET 404 → 唤醒同步 → 轮询 ≤3 次
  （间隔 _poll_sleep(2)）→ 再判定；返回云端是否可达该会话。
- fail-closed：无绑定 → ``LinkUnavailable``；登录失败 → ``LinkAuthError``；
  永不假成功、不静默改道。

测试缝（MockTransport 注入）：``_load_sync_binding``（默认 None，生产走
``_load_sync_binding_async``）· ``_get_client`` · ``_wake_sync`` · ``_poll_sleep``。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "CloudLinkService",
    "CloudBinding",
    "LinkUnavailable",
    "LinkAuthError",
    "cloud_link_service",
]


class LinkUnavailable(Exception):
    """无云端绑定 / 云端不可达（fail-closed：中止中继，不落本地执行）。"""


class LinkAuthError(Exception):
    """云端凭据失效（登录 401/失败）——提示用户在「云端关联」重新绑定。"""


class CloudBinding:
    """SyncState 快照（纯值对象）。"""

    __slots__ = ("server_url", "cloud_username", "cloud_password", "tls_verify")

    def __init__(self, server_url: str, cloud_username: str, cloud_password: str,
                 tls_verify: bool = True):
        self.server_url = server_url
        self.cloud_username = cloud_username
        self.cloud_password = cloud_password  # vw-approved: 构造器形参赋值，非字面密钥
        self.tls_verify = tls_verify


def _load_sync_binding(user_id: str) -> Optional[CloudBinding]:
    """测试缝（单测 monkeypatch 本函数返回假绑定）；生产默认 None → 走 async 读。"""
    return None


async def _load_sync_binding_async(user_id: str) -> Optional[CloudBinding]:
    """读该用户的云端绑定（SyncState 行）。"""
    from sqlalchemy import select

    from app.db.database import AsyncSessionLocal, SyncState

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(SyncState).where(SyncState.user_id == user_id))
        ).scalar_one_or_none()
        if row is None:
            return None
        url = (getattr(row, "server_url", "") or "").strip()
        username = (getattr(row, "cloud_username", "") or "").strip()
        password = getattr(row, "cloud_password", "") or ""
        if not url or not username or not password:
            return None
        tls_verify = getattr(row, "tls_verify", True)
        return CloudBinding(
            url.rstrip("/"), username, password,
            tls_verify=True if tls_verify is None else bool(tls_verify),
        )


_shared_client: Optional[httpx.AsyncClient] = None
_insecure_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    """中继用共享 httpx 客户端。测试缝：monkeypatch 返回 MockTransport 客户端。"""
    global _shared_client
    if _shared_client is None:
        # trust_env=False：macOS 系统代理会劫持 httpx（D-128 同类）；云端
        # 绑定走到自建 60 时不应经系统代理。
        _shared_client = httpx.AsyncClient(
            follow_redirects=True,
            trust_env=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    return _shared_client


def _client_for(binding: "CloudBinding") -> httpx.AsyncClient:
    """按绑定 tls_verify 选客户端（60 自签证书 = false）。测试缝保持 _get_client。"""
    if getattr(binding, "tls_verify", True):
        return _get_client()
    global _insecure_client
    if _insecure_client is None:
        _insecure_client = httpx.AsyncClient(
            follow_redirects=True,
            trust_env=False,
            verify=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    return _insecure_client


def _wake_sync() -> None:
    """唤醒同步周期（把本地新建会话尽快推上云端）。测试缝：monkeypatch。"""
    try:
        from app.services.sync_client import sync_client

        sync_client.wake()
    except Exception:
        logger.warning("cloud_link: sync wake failed", exc_info=True)


async def _poll_sleep(seconds: float) -> None:
    """轮询间隔。测试缝：monkeypatch 为 asyncio.sleep(0)。"""
    await asyncio.sleep(seconds)


class CloudLinkService:
    """云端请求面（登录/令牌/401 自愈/会话存在性保障）。"""

    def __init__(self) -> None:
        self._tokens: dict = {}  # user_id → JWT（内存缓存，401 重登）

    async def _binding(self, user_id: str) -> CloudBinding:
        binding = _load_sync_binding(user_id)
        if binding is None:
            binding = await _load_sync_binding_async(user_id)
        if binding is None:
            raise LinkUnavailable("该账号未绑定云端（「云端关联」未配置）")
        return binding

    async def is_linked(self, user_id: str) -> bool:
        try:
            await self._binding(user_id)
            return True
        except Exception:
            return False

    async def _login(self, user_id: str, binding: CloudBinding) -> str:
        client = _client_for(binding)
        try:
            resp = await client.post(
                f"{binding.server_url}/api/auth/login",
                json={
                    "username": binding.cloud_username,
                    "password": binding.cloud_password,  # vw-approved: 传递已存绑定凭据，非字面密钥
                },
                timeout=20.0,
            )
        except httpx.HTTPError as exc:
            raise LinkUnavailable(f"云端登录请求失败：{exc}") from exc
        if resp.status_code != 200:
            raise LinkAuthError(
                f"云端登录失败（HTTP {resp.status_code}）：请在「云端关联」核对账号密码后重新绑定"
            )
        try:
            token = resp.json().get("access_token") or ""
        except Exception:
            token = ""
        if not token:
            raise LinkAuthError("云端登录响应缺少 access_token：请重新绑定云端账号")
        self._tokens[user_id] = token
        return token

    async def request(
        self,
        user_id: str,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[dict] = None,
        timeout: float = 30.0,
        stream: bool = False,
        content: Optional[bytes] = None,
        extra_headers: Optional[dict] = None,
    ) -> httpx.Response:
        """带云端身份的请求（自动登录；401 重登一次重试）。path 以 /api 开头。
        stream=True 返回流式响应（调用方 aiter_bytes 泵完后 aclose）。"""
        if not path.startswith("/api"):
            raise ValueError("cloud_link path 须以 /api 开头")
        binding = await self._binding(user_id)
        client = _client_for(binding)
        token = self._tokens.get(user_id) or await self._login(user_id, binding)
        url = f"{binding.server_url}{path}"

        async def _send(tok: str) -> httpx.Response:
            headers = {"Authorization": f"Bearer {tok}", **(extra_headers or {})}
            req = client.build_request(
                method, url,
                json=json,
                params=params,
                content=content,
                headers=headers,
                timeout=timeout,
            )
            return await client.send(req, stream=stream)

        try:
            resp = await _send(token)
        except httpx.HTTPError as exc:
            raise LinkUnavailable(f"云端不可达：{exc}") from exc
        if resp.status_code == 401:
            # token 过期/轮换：重登一次重试（第二次 401 即 LinkAuthError，不无限重试）
            if stream:
                await resp.aclose()
            token = await self._login(user_id, binding)
            try:
                resp = await _send(token)
            except httpx.HTTPError as exc:
                raise LinkUnavailable(f"云端不可达：{exc}") from exc
            if resp.status_code == 401:
                if stream:
                    await resp.aclose()
                raise LinkAuthError("云端鉴权失败（重登后仍 401）：请重新绑定云端账号")
        return resp

    async def ensure_conversation(self, user_id: str, conv_id: str) -> bool:
        """确保云端存在该会话：404 → 唤醒同步 → 轮询 ≤3 次 → 再判定。"""
        resp = await self.request(
            user_id, "GET", f"/api/conversations/{conv_id}", timeout=15.0
        )
        if resp.status_code == 200:
            return True
        if resp.status_code != 404:
            logger.warning(
                "cloud_link: ensure_conversation unexpected HTTP %s for %s",
                resp.status_code, conv_id,
            )
            return False
        # 云端尚无该会话（本地新建未推上）：唤醒同步推一轮，再有界轮询
        _wake_sync()
        for _ in range(3):
            await _poll_sleep(2.0)
            resp = await self.request(
                user_id, "GET", f"/api/conversations/{conv_id}", timeout=15.0
            )
            if resp.status_code == 200:
                return True
        return False


cloud_link_service = CloudLinkService()
