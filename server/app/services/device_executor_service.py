"""device executor worker — 被云端 agent 驱动的本地工具执行入口（client-only）。

PLAN_device_executor §1.1/§2.1/§2.2；协议契约=chatbot 主方案 §5（信封/方法/
语义），安全模型=§8.4（设备侧防线不依赖云端正确性）。

- 出站 WSS 连云端 `/api/device/ws`，握手 `Authorization: Bearer <device token>`
  头（长效凭据不进 URL/日志）；断线指数退避重连（cap 60s）+ heartbeat 通知。
- W16b 信封（Consistency Hub 修订行=上游已落地契约为准）：出站全带 `"jsonrpc": "2.0"`，
  请求 id ≡ call_id；`tools/call`/`initialize` 回 `result {ok, content}` /
  `{ok: false, error}`，协议级失败走 `error {code, message}` 信封；
  大文件 `chunk {transfer_id, seq, data_b64, sha256, last}`（末块 last:true）。
- 安全（§2.1）：入站 args 丢弃一切 `_` 前缀控制键；`remote_call_scope()` 内
  权限门与包含性检查无条件执行；`allowed_tools` 白名单镜像 fail-closed；
  `workspace_ctx` 仅做逻辑 workspace 名严格对齐，路径一律 workspace 相对。
- 语义：at-most-once（断线取消在途调用，不重放）；`tools/cancel` → asyncio
  任务取消 + 既有会话/任务管理器杀进程。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import platform as platform_mod
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import websockets

from app.core.config import get_config
from app.services.device_pairing_service import load_binding

logger = logging.getLogger(__name__)

# ---- Consistency Hub：协议常量（改名先改 Hub 再 grep 旧拼写至零命中）----
WS_PATH = "/api/device/ws"
PROTOCOL_VERSION = 1
CHUNK_SIZE = 256 * 1024
CLOSE_POLICY_VIOLATION = 1008
# progress 发报规范（下游任务单 DOWNSTREAM_TODO_20261011 §一）：
# 间隔 min(30s, 窗/3)（下限 1s=≤1 次/秒）；note 单行 ≤60 无控制字符。
PROGRESS_INTERVAL_CAP = 30.0
PROGRESS_INTERVAL_FLOOR = 1.0
PROGRESS_NOTE_MAX = 60


def _progress_interval(window: float) -> float:
    """发报间隔=min(30s, 窗/3)（任务单 §一.2）；窗非法按 120s 缺省。"""
    try:
        w = float(window)
    except (TypeError, ValueError):
        w = 120.0
    if not (w > 0):
        w = 120.0
    return max(PROGRESS_INTERVAL_FLOOR, min(PROGRESS_INTERVAL_CAP, w / 3.0))


def _sanitize_note(note: Any) -> str:
    """note=单行人类可读（≤60、无控制字符；任务单 §一.3）——发报前消毒。

    isprintable 过滤同时清掉 C0/C1 控制符与 bidi/零宽等格式字符（评审 B-M3）。
    """
    text = "".join(ch if ch.isprintable() else " " for ch in str(note or ""))
    return " ".join(text.split())[:PROGRESS_NOTE_MAX]


def _effective_timeout(requested: Any, local_default: float) -> float:
    """tools/call 等待上限 = max(本地默认, 请求值)（PLAN §1.1）。"""
    try:
        req = float(requested or 0)
    except (TypeError, ValueError):
        req = 0.0
    return max(float(local_default), req)


def _ws_url(cloud_url: str) -> str:
    base = (cloud_url or "").rstrip("/")
    if base.startswith("ws://") or base.startswith("wss://"):
        return base + WS_PATH
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + WS_PATH
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):] + WS_PATH
    return base + WS_PATH


def _strip_control_keys(args: Dict[str, Any]) -> Dict[str, Any]:
    """fail-closed 剥离 `_` 前缀内部控制键（同 agent_loop._strip_internal_tool_args）。"""
    if not isinstance(args, dict):
        return {}
    return {k: v for k, v in args.items() if not str(k).startswith("_")}


def _swallow_late(t: "asyncio.Task") -> None:
    """排空逾期异常，避免 GC 时 "never retrieved" 噪音（build/ticker 通用）。"""
    if not t.cancelled():
        t.exception()


class DeviceExecutorWorker:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stopped = True
        self._wake: asyncio.Event = asyncio.Event()
        self._ws: Any = None
        self._connected = False
        self._auth_failed = False
        self._last_error = ""
        self._last_heartbeat_at: Optional[str] = None
        self._conn_token: str = ""
        self._done_calls: set = set()       # 已应答 call_id 去重史
        self._done_order: list = []          # FIFO 顺序（评审 minor：set 裁剪无序）
        self._max_inflight = 8
        self._max_heavy_fs = 4       # fs 重活（zip/office-*）并发上限（D-92；轻量 fs 不设限）
        self._heavy_fs_inflight = 0
        self._fs_tasks: Dict[str, asyncio.Task] = {}  # fs/* 任务随连接取消（评审 NEW-1）；键=请求 id（W19 起 tools/cancel 可回指）
        # call_id -> {"id": req_id, "task": Task, "tool": str, "args": dict, "replied": bool}
        self._inflight: Dict[str, Dict[str, Any]] = {}

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        if not get_config().client_exec_enabled:
            logger.info("device executor disabled ([client_exec] enabled=false)")
            return
        if self._task is not None:
            return
        self._stopped = False
        self._auth_failed = False
        self._task = asyncio.create_task(self._run())
        logger.info("device executor worker started")

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        await self._cancel_inflight("worker stop")
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._connected = False
        await self._drain_fs_tasks()
        logger.info("device executor worker stopped")

    async def _drain_fs_tasks(self) -> None:
        """收束 fs/* 任务：取消后有界等待（评审 A#4/B#2：cancel 后不 await=收束不可证）。"""
        tasks = list(self._fs_tasks.values())
        for t in tasks:
            t.cancel()
        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait(tasks, timeout=5)
        self._fs_tasks.clear()

    def wake(self) -> None:
        """配对/重置后立即唤醒主循环重读凭据（仿 sync_client.wake）。

        清除 _auth_failed 单向闩（重置/重配对后必须可重连——评审 A#1）并
        若凭据已撤销则主动断开在途连接（reset=断连+拒新调用，评审 B#1）。
        """
        self._auth_failed = False
        binding = load_binding()
        token = str((binding or {}).get("token") or "")
        if token != self._conn_token and self._ws is not None:
            # 不以 _connected 为门（评审修复：服务端 handler 先于客户端置位——竞态漏踢）
            self._kick("credential changed or cleared")
        self._wake.set()

    def _kick(self, reason: str) -> None:
        """主动断开当前连接（reset/凭据变更）——不等云端或心跳超时。"""
        ws = self._ws
        logger.info("device executor connection kicked: %s", reason)
        if ws is not None:
            asyncio.ensure_future(self._safe_close(ws))

    @staticmethod
    async def _safe_close(ws: Any) -> None:
        try:
            await ws.close()
        except Exception:
            pass

    def status(self) -> Dict[str, Any]:
        binding = load_binding() or {}
        config = get_config()
        return {
            "enabled": config.client_exec_enabled,
            "paired": bool(binding.get("token")),
            "connected": self._connected,
            "auth_failed": self._auth_failed,
            "device_id": binding.get("device_id", ""),
            "name": binding.get("name", ""),
            "platform": binding.get("platform", ""),
            "cloud_url": binding.get("cloud_url") or config.client_exec_cloud_url,
            "last_heartbeat_at": self._last_heartbeat_at,
            "last_error": self._last_error,
            "inflight_calls": len(self._inflight),
            # W20：执行身份是否已绑定（local_user_id 或云端身份任一在档）。
            # False=身份解析将 fail-closed（identity_unbound）——面板提示重新配对。
            "identity_bound": bool(
                binding.get("local_user_id")
                or (binding.get("cloud_username") and binding.get("cloud_user_id"))
            ),
        }

    # ---------- 连接循环 ----------
    async def _idle_wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        # 消费唤醒后才 clear（评审 A#9：先 clear 会吞掉窗外到达的 wake）
        self._wake.clear()

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopped:
            binding = load_binding()
            if not binding or self._auth_failed:
                # 未配对/凭据已废：空转待 wake（reset/重新配对），不空打云端
                await self._idle_wait(15.0)
                continue
            token = str(binding.get("token") or "")
            self._conn_token = token
            url = _ws_url(str(binding.get("cloud_url") or get_config().client_exec_cloud_url))
            headers = {"Authorization": f"Bearer {token}"}
            ssl_ctx = None
            if url.startswith("wss://") and not binding.get("tls_verify", True):
                import ssl as ssl_mod

                ssl_ctx = ssl_mod._create_unverified_context()
            try:
                async with websockets.connect(
                    url,
                    additional_headers=headers,
                    ssl=ssl_ctx,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._last_error = ""
                    self._auth_failed = False
                    self._done_calls.clear()
                    self._done_order.clear()
                    backoff = 1.0
                    # 建连即复核凭据（reset/重配对与建连的竞态兜底）
                    if str((load_binding() or {}).get("token") or "") != self._conn_token:
                        self._connected = False
                        self._ws = None
                        await self._safe_close(ws)
                        continue
                    logger.info("device executor connected to %s", url)
                    try:
                        await self._session(ws)
                    finally:
                        self._connected = False
                        self._ws = None
                        await self._cancel_inflight("connection closed")
            except websockets.exceptions.InvalidHandshake as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                self._last_error = f"handshake failed: {exc}"
                if status in (401, 403):
                    self._auth_failed = True
                    logger.warning("device token rejected (HTTP %s) — awaiting re-pair", status)
            except websockets.exceptions.ConnectionClosed as exc:
                self._last_error = f"closed: {exc}"
                if exc.rcvd is not None and exc.rcvd.code == CLOSE_POLICY_VIOLATION:
                    self._auth_failed = True
                    logger.warning("device token revoked (close 1008) — awaiting re-pair")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("device executor connection error: %s", exc)
            if self._stopped:
                break
            await self._idle_wait(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _session(self, ws: Any) -> None:
        hb = asyncio.create_task(self._heartbeat_loop(ws))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("device executor: non-JSON frame dropped")
                    continue
                if not isinstance(msg, dict):
                    continue
                if "method" not in msg:
                    continue
                method = str(msg.get("method"))
                if method == "tools/call":
                    self._spawn_tools_call(msg)
                elif method in ("fs/list", "fs/read", "fs/download",
                                "fs/zip", "fs/office-html", "fs/office-pdf"):
                    # fs/* 与收发循环解耦（A#7），但任务随连接取消且应答走发起时 ws
                    # （评审 NEW-1：不得跨连接回放旧请求的字节/信封）。
                    # 键=请求 id（上游 tools/cancel 以 call_id=请求 id 回指，W19 起可取消）
                    key = str(msg.get("id"))
                    task = asyncio.ensure_future(self._handle_message(ws, msg))
                    self._fs_tasks[key] = task

                    def _done(t: asyncio.Task, k: str = key) -> None:
                        # 身份核对（复评：同 id 后到任务不得被先完成者的回调误逐出）
                        if self._fs_tasks.get(k) is t:
                            self._fs_tasks.pop(k, None)

                    task.add_done_callback(_done)
                else:
                    await self._handle_message(ws, msg)
        finally:
            hb.cancel()
            await self._drain_fs_tasks()
            try:
                await hb
            except (asyncio.CancelledError, Exception):
                pass

    async def _heartbeat_loop(self, ws: Any) -> None:
        interval = max(5, int(get_config().client_exec_heartbeat_seconds))
        while True:
            await asyncio.sleep(interval)
            try:
                await self._send({
                    "method": "heartbeat",
                    "params": {
                        "device_id": self.status().get("device_id", ""),
                        "ts": datetime.now(timezone.utc).isoformat(),
                    },
                }, ws=ws)
                self._last_heartbeat_at = datetime.now(timezone.utc).isoformat()
            except Exception:
                return

    # ---------- 消息处理 ----------
    async def _handle_message(self, ws: Any, msg: Dict[str, Any]) -> None:
        req_id = msg.get("id")
        method = str(msg.get("method"))
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method == "initialize":
            caps = {
                "protocol_version": PROTOCOL_VERSION,
                "device_id": self.status().get("device_id", ""),
                "name": self.status().get("name", ""),
                "platform": platform_mod.platform(),
                "tools": list(get_config().client_exec_allowed_tools),
                "max_payload_mb": int(get_config().client_exec_max_payload_mb),
                "chunking": True,
            }
            await self._reply_result(req_id, params.get("call_id"),
                                     {"ok": True, "content": json.dumps(caps, ensure_ascii=False)})
        elif method == "tools/cancel":
            await self._handle_tools_cancel(req_id, params)
        elif method == "fs/list":
            await self._handle_fs(ws, req_id, params, "list")
        elif method == "fs/read":
            await self._handle_fs(ws, req_id, params, "read")
        elif method == "fs/download":
            await self._handle_fs(ws, req_id, params, "download")
        elif method == "fs/zip":
            await self._handle_fs(ws, req_id, params, "zip")
        elif method == "fs/office-html":
            await self._handle_fs(ws, req_id, params, "office-html")
        elif method == "fs/office-pdf":
            await self._handle_fs(ws, req_id, params, "office-pdf")
        elif method == "heartbeat":
            self._last_heartbeat_at = datetime.now(timezone.utc).isoformat()
            if req_id is not None:
                await self._reply_result(req_id, params.get("call_id"), {"pong": True})
        else:
            await self._reply_error(req_id, req_id, "method_not_found", f"unknown method: {method}")

    def _spawn_tools_call(self, msg: Dict[str, Any]) -> None:
        req_id = msg.get("id")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        # 上游契约（W16b Hub）：id ≡ call_id，params 不含 call_id；
        # tools/cancel 以 params.call_id 回指本键。缺 id 时才自造（宽容收尾）。
        call_id = str(req_id if req_id is not None else (params.get("call_id") or uuid.uuid4().hex))
        if call_id in self._inflight or call_id in self._done_calls:
            asyncio.ensure_future(self._reply_error(
                req_id, call_id, "duplicate_call_id", f"call_id already used/in flight: {call_id}"))
            return
        if len(self._inflight) >= self._max_inflight:
            asyncio.ensure_future(self._reply_error(
                req_id, call_id, "too_many_in_flight", f"in-flight cap {self._max_inflight} reached"))
            return
        task = asyncio.ensure_future(self._do_tools_call(req_id, call_id, params))
        self._inflight[call_id] = {
            "id": req_id,
            "task": task,
            "tool": str(params.get("tool") or ""),
            "args": params.get("args") if isinstance(params.get("args"), dict) else {},
            "owner_id": "",
            "replied": False,
        }

        def _done(t: asyncio.Task, cid: str = call_id) -> None:
            self._inflight.pop(cid, None)
            self._remember_call(cid)

        task.add_done_callback(_done)

    def _binding_alive(self, binding: Optional[Dict[str, Any]] = None) -> bool:
        """连接凭据仍有效：store 存在且 token 与本连接一致（reset/重配对即拒新调用）。
        binding=调用发起时快照（None=自行读取）。"""
        binding = binding if binding is not None else load_binding()
        return bool(binding) and str(binding.get("token") or "") == self._conn_token

    async def _do_tools_call(self, req_id: Any, call_id: str, params: Dict[str, Any]) -> None:
        config = get_config()
        entry = self._inflight.get(call_id) or {"id": req_id, "replied": False}
        binding = load_binding() or {}  # 调用发起时快照（评审 r2-F7：换绑不偷换在途身份）
        if not self._binding_alive(binding):
            await self._reply_call(entry, call_id, ok=False, code="unauthorized",
                                   message="credential revoked or reset")
            return
        tool = str(params.get("tool") or "")
        args = _strip_control_keys(params.get("args") if isinstance(params.get("args"), dict) else {})
        if tool not in config.client_exec_allowed_tools:
            await self._reply_call(entry, call_id, ok=False, code="tool_not_allowed",
                                   message=f"tool not in device allowlist: {tool}")
            return
        effective = _effective_timeout(params.get("timeout_seconds"), config.client_exec_call_timeout_seconds)

        try:
            owner, ws_root, expected = await self._resolve_identity(binding)
        except IdentityUnboundError as exc:
            await self._reply_call(entry, call_id, ok=False, code="identity_unbound",
                                   message=str(exc)[:200])
            return
        except Exception:
            logger.exception("tools/call identity resolve failed")
            await self._reply_call(entry, call_id, ok=False, code="workspace_unavailable",
                                   message="工作区暂不可用")
            return
        entry["owner_id"] = str(getattr(owner, "id", "") or "")

        # W20 名门：期望值=云端身份导出（两侧同源）。
        # 仅「键缺省」（真旧云端）放行；键在但 None/空串/非字符串=fail-closed
        # （评审 r2-F3/r1-F2：falsy 不得冒充缺省绕过名门）。
        if "workspace_ctx" in params:
            ws_ctx = params.get("workspace_ctx")
            if not (isinstance(ws_ctx, str) and ws_ctx):
                await self._reply_call(entry, call_id, ok=False, code="workspace_mismatch",
                                       message="workspace_ctx missing or empty")
                return
            if expected is None:
                await self._reply_call(entry, call_id, ok=False, code="identity_unbound",
                                       message="设备未绑定云端执行身份（请重新配对或重新提交同步绑定）")
                return
            if ws_ctx != expected:
                await self._reply_call(entry, call_id, ok=False, code="workspace_mismatch",
                                       message=f"workspace_ctx {ws_ctx!r} != expected {expected!r}")
                return

        script = params.get("script")
        # 建议档覆盖（任务单 §一.4）：长 tools/call 发 progress（快速调用首 tick 前完成=自然不发）。
        # 窗=请求 timeout_seconds（=云端 `_tool_timeout` 实际等待窗；评审 A-I1：不得用
        # max(effective,120) 顶大——小窗下调时首 tick 须仍 ≤ 窗/2 才能续期命中）。
        tool_ticker = None
        tool_started = time.monotonic()
        try:
            tick_window = float(params.get("timeout_seconds") or 0) or effective
        except (TypeError, ValueError):
            tick_window = effective  # 防御：畸形帧不得让 _do_tools_call 无应答（评审 r2 I-1）
        if req_id is not None:
            tool_ticker = asyncio.ensure_future(self._progress_ticker(
                None, str(req_id),
                lambda: f"{tool} {time.monotonic() - tool_started:.0f}s",
                tick_window))
            tool_ticker.add_done_callback(_swallow_late)

        def _stop_tool_ticker() -> None:
            if tool_ticker is not None:
                tool_ticker.cancel()  # 终态即停发（评审 A-M1：先停再回包）

        try:
            if tool == "skill_run_script" and isinstance(script, str) and script:
                payload = await asyncio.wait_for(
                    self._run_inline_script(Path(ws_root), args, script,
                                            str(params.get("script_ext") or "")),
                    timeout=effective,
                )
            else:
                payload = await asyncio.wait_for(
                    self._dispatch(tool, args, owner, ws_root),
                    timeout=effective,
                )
        except asyncio.TimeoutError:
            _stop_tool_ticker()
            await self._reply_call(entry, call_id, ok=False, code="timeout",
                                   message=f"tool call exceeded {effective:.0f}s")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _stop_tool_ticker()
            logger.exception("tools/call %s failed", tool)
            # 固定错误串不回显本地路径（W16 教训 19 / 复评 NEW-3）
            await self._reply_call(entry, call_id, ok=False, code="internal_error",
                                   message="tool execution failed")
            return
        finally:
            _stop_tool_ticker()
        await self._reply_call(entry, call_id, ok=True, payload=payload)

    async def _dispatch(self, tool: str, args: Dict[str, Any], owner: Any, ws_root: str) -> str:
        from app.db.database import AsyncSessionLocal
        from app.services.agent_permissions import PermissionContext
        from app.tools.execution_context import remote_call_scope
        from app.tools.registry import registry

        # §2.1：strict 权限上下文（必非空——否则 registry 权限闸整体跳过）
        perm_ctx = PermissionContext(
            super_admin_bypass=False,
            deathmatch_active=False,
            user=owner,
        )
        async with AsyncSessionLocal() as db:
            with remote_call_scope():
                return await registry.dispatch(
                    tool,
                    args,
                    db=db,
                    user=owner,
                    workspace_path=ws_root,
                    permission_context=perm_ctx,
                    allowed_tools=get_config().client_exec_allowed_tools,
                )

    async def _run_inline_script(self, ws_root: Path, args: Dict[str, Any],
                                 content: str, script_ext: str) -> str:
        """skill_run_script 内联脚本（W16b：script=纯内容串 + script_ext 扩展名）：
        物化 {workspace}/skill_scripts/ → _exec_skill_script。"""
        from app.tools.skill_tools import _INTERP_BY_EXT, _exec_skill_script

        ext = ("." + script_ext.lstrip(".")).lower() if script_ext.strip() else ".py"
        interpreter = _INTERP_BY_EXT.get(ext)
        if not interpreter:
            return json.dumps({"error": f"unsupported script type: {ext}"}, ensure_ascii=False)
        stem = Path(str(args.get("script_path") or "inline")).stem or "inline"
        if stem in ("", ".", "..") or "\x00" in stem:
            stem = "inline"
        filename = stem + ext
        dest_dir = ws_root / "skill_scripts"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        dest.write_text(content, encoding="utf-8")
        meta = {"script": filename, "skill": str(args.get("skill_name") or "inline")}
        from app.tools.execution_context import remote_call_scope

        with remote_call_scope():
            return await _exec_skill_script(
                dest,
                interpreter,
                str(args.get("args") or ""),
                float(args.get("timeout") or 30),
                str(ws_root),
                meta,
            )

    async def _handle_tools_cancel(self, req_id: Any, params: Dict[str, Any]) -> None:
        raw_cid = params.get("call_id")
        call_id = str(raw_cid) if raw_cid is not None else ""
        # W19：fs/*（zip/office 转换等重活）同样可被 tools/cancel 中止——上游 RPC
        # 超时即发此信号（device_session_service：「超时尽力通知设备取消」）。
        fs_task = self._fs_tasks.pop(call_id, None)
        if fs_task is not None:
            fs_task.cancel()
            self._remember_call(call_id)  # 取消后 id 入去重窗（与 tools/call 面一致）
            await self._reply_result(req_id, call_id, {"cancelled": True})
            return
        entry = self._inflight.get(call_id)
        if entry is None:
            await self._reply_error(req_id, call_id, "call_not_found", f"no in-flight call: {call_id}")
            return
        tool = entry.get("tool", "")
        args = entry.get("args") or {}
        entry["task"].cancel()
        # 既有会话/任务管理器杀进程（PLAN §1.1 tools/cancel 映射）。
        # user_id 用属主 id（评审 A#3：args.user_id 并非 process 工具入参）。
        if tool == "process" and args.get("session_id") and entry.get("owner_id"):
            try:
                from app.services.process_session_service import process_manager

                await process_manager.kill(str(entry["owner_id"]), str(args.get("session_id")))
            except Exception:
                logger.warning("cancel: process session kill failed", exc_info=True)
        await self._reply_result(req_id, call_id, {"cancelled": True})
        # 原 tools/call 请求收到 cancelled 应答（云端 Future 收敛，at-most-once；
        # _reply_call 的 replied 互斥挡与完成路径的竞态双应答）
        await self._reply_call(entry, call_id, ok=False, code="cancelled", message="call cancelled")
        self._inflight.pop(call_id, None)
        self._remember_call(call_id)

    async def _cancel_inflight(self, reason: str) -> None:
        """断线/停止：at-most-once——在途调用取消且不重放。"""
        entries = list(self._inflight.items())
        self._inflight.clear()
        for call_id, entry in entries:
            entry["task"].cancel()
            entry["replied"] = True
            logger.info("in-flight call %s cancelled (%s)", call_id, reason)

    # ---------- fs/*（§2.2 路径契约）----------
    def _resolve_in_workspace(self, ws_root: Path, rel: str) -> Path:
        from app.services.workspace_paths import CODE_INVALID, CODE_OUTSIDE, WorkspacePathError, is_within

        text = str(rel or "").strip()
        if not text or "\x00" in text:
            raise WorkspacePathError(CODE_INVALID, "empty or invalid path", text)
        if text.startswith("/") or text.startswith("\\") or (len(text) > 1 and text[1] == ":"):
            raise WorkspacePathError(CODE_INVALID, "absolute path not allowed", text)
        target = (ws_root / text).resolve()
        if not is_within(ws_root, target):
            raise WorkspacePathError(CODE_OUTSIDE, "path escapes workspace", text)
        return target

    def _fs_binding(self) -> Dict[str, Any]:
        return load_binding() or {}

    async def _handle_fs(self, ws: Any, req_id: Any, params: Dict[str, Any], op: str) -> None:
        from app.services.workspace_paths import WorkspacePathError

        call_id = params.get("call_id")
        config = get_config()
        binding = self._fs_binding()  # 单一快照（复评 NEW-3 附注）
        if not self._binding_alive(binding):
            await self._reply_result(req_id, call_id,
                                     {"ok": False, "error": "unauthorized: credential revoked or reset"}, ws=ws)
            return
        try:
            _, ws_root_str, _expected = await self._resolve_identity(binding)
            ws_root = Path(ws_root_str)
            rel = str(params.get("path") or "").strip()
            if op == "list":
                from app.services.workspace_files_service import list_directory

                # 空 path = 工作区根（评审 A#10）
                self._resolve_in_workspace(ws_root, rel or ".")
                out = list_directory(ws_root, rel or ".", offset=int(params.get("offset") or 0),
                                     limit=int(params.get("limit") or 1000))
                out["ok"] = True
                await self._reply_result(req_id, call_id, out, ws=ws)
                return
            target = self._resolve_in_workspace(ws_root, rel)
            if op == "read":
                max_bytes = int(config.client_exec_max_payload_mb) * 1024 * 1024
                offset = max(0, int(params.get("offset") or 0))
                limit = min(max(0, int(params.get("limit") or max_bytes)), max_bytes)
                if target.is_dir():
                    await self._reply_result(req_id, call_id, {"ok": False, "error": "workspace_path_invalid: path is a directory"}, ws=ws)
                    return
                with target.open("rb") as f:
                    total = f.seek(0, 2)  # 真实尺寸（stat 前置读有 TOCTOU——评审 minor）
                    f.seek(offset)
                    window = f.read(limit)
                await self._reply_result(req_id, call_id, {
                    "ok": True,
                    "path": rel,
                    "offset": offset,
                    "limit": limit,
                    "total": total,
                    "eof": len(window) < limit,
                    "encoding": "utf-8",
                    "content": window.decode("utf-8", errors="replace"),
                }, ws=ws)
            elif op in ("zip", "office-html", "office-pdf"):
                # D-92 重活并发闸（打包线程/soffice/临时件）：超限即时拒，不排队放大资源占用；
                # 轻量 fs/*（list/read/download）走上面分支不受限。
                if self._heavy_fs_inflight >= self._max_heavy_fs:
                    await self._reply_result(req_id, call_id, {"ok": False, "error": (
                        f"too_many_in_flight: heavy fs cap {self._max_heavy_fs} reached")}, ws=ws)
                    return
                self._heavy_fs_inflight += 1
                try:
                    if op == "zip":
                        if target.exists() and not target.is_dir():
                            await self._reply_result(req_id, call_id, {
                                "ok": False, "error": "workspace_path_invalid: path is not a directory"}, ws=ws)
                            return
                        await self._send_zip_dir(ws, req_id, call_id, ws_root, rel)
                    else:
                        await self._send_office_converted(
                            ws, req_id, call_id, target, config,
                            kind="html" if op == "office-html" else "pdf")
                finally:
                    self._heavy_fs_inflight -= 1
            else:  # download
                # 建议档覆盖（任务单 §一.4）：大文件/慢盘传输发 progress（首 tick 前完成=自然不发）
                dl_ticker = None
                if req_id is not None:
                    dl_ticker = asyncio.ensure_future(self._progress_ticker(
                        ws, str(req_id),
                        lambda: f"download {self._tmp_size(str(target))}",
                        float(config.client_exec_call_timeout_seconds)))
                    dl_ticker.add_done_callback(_swallow_late)
                try:
                    await self._send_chunks(ws, req_id, call_id, target)
                finally:
                    if dl_ticker is not None:
                        dl_ticker.cancel()
        except WorkspacePathError as exc:
            # exc.__str__ 已含 code（"{code}: {message}"），勿再前缀（曾双写）
            await self._reply_result(req_id, call_id, {"ok": False, "error": str(exc)}, ws=ws)
        except FileNotFoundError:
            await self._reply_result(req_id, call_id, {"ok": False, "error": "workspace_path_not_found: file not found"}, ws=ws)
        except asyncio.CancelledError:
            raise
        except IdentityUnboundError as exc:
            await self._reply_result(req_id, call_id,
                                     {"ok": False, "error": f"identity_unbound: {str(exc)[:160]}"}, ws=ws)
        except Exception:
            logger.exception("fs/%s failed", op)
            # 固定错误串不回显本地路径（W16 教训 19 / 评审 r1-F8）
            await self._reply_result(req_id, call_id,
                                     {"ok": False, "error": "internal_error: fs operation failed"}, ws=ws)

    async def _progress_ticker(self, ws: Any, call_id: str, note_fn: Any, window: float) -> None:
        """长任务活性通知：按 `_progress_interval(window)` 周期发 `progress {call_id, note}`。

        协议既有通知面（API.md；云端 `_renew_progress_deadline` 按 call_id 续期等待窗）。
        call_id=请求 id 逐字回显（上游 `str(params.call_id)` 对 pending_waits 键匹配）；
        note 经 `_sanitize_note`；任务终态即停发（生产端负责取消本 ticker）。
        """
        interval = _progress_interval(window)
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    note = note_fn() if callable(note_fn) else str(note_fn or "")
                except Exception:  # note_fn 故障不得杀死发报循环（评审 B-M4：静默断续期）
                    logger.debug("progress note_fn failed", exc_info=True)
                    note = ""
                await self._send({"method": "progress", "params": {
                    "call_id": call_id, "note": _sanitize_note(note)}}, ws=ws)
        except asyncio.CancelledError:
            raise

    @staticmethod
    def _tmp_size(path: str) -> str:
        try:
            return f"{os.path.getsize(path) // 1024}KB"
        except OSError:
            return "…"

    async def _send_zip_dir(self, ws: Any, req_id: Any, call_id: Any,
                            ws_root: Path, rel: str) -> None:
        """fs/zip（W19）：打包目录→临时 zip→`fs/download` 同形通道（单包/分块）。

        产物走 :meth:`_send_chunks`，result 形状与 fs/download 逐键一致（Consistency
        Hub）。取消路径**不中断打包线程**（线程不可中断）：等其收尾（30s 兜底）再删
        临时件，否则线程后写会复活已删文件成孤儿；临时件前缀 `wt_ws_zip_` 与
        files.py 同词表。
        """
        from app.services.workspace_files_service import (
            build_folder_zip,
            prune_stale_zips,
        )

        await asyncio.to_thread(prune_stale_zips)  # 评审 A#5/B#5：崩溃遗留整包自愈
        fd, tmp_name = tempfile.mkstemp(prefix="wt_ws_zip_", suffix=".zip")
        os.close(fd)
        build = asyncio.ensure_future(
            asyncio.to_thread(build_folder_zip, ws_root, rel, tmp_name))
        build.add_done_callback(_swallow_late)
        budget = float(get_config().client_exec_call_max_extension_seconds or 0.0) or None
        window = float(get_config().client_exec_call_timeout_seconds)
        ticker = None
        if req_id is not None:
            ticker = asyncio.ensure_future(self._progress_ticker(
                ws, str(req_id),
                lambda: f"zip {self._tmp_size(tmp_name)}",
                window))
            ticker.add_done_callback(_swallow_late)
        try:
            try:
                # shield：Task.cancel()/wait_for 超时会连带取消当前 await 的 future（_fut_waiter），
                # 而打包线程不可中断——裸 await 会让取消把 build 标记完成、线程继续写成孤儿。
                # 预算=call_max_extension_seconds（云端续期硬上限镜像；0=不限制，交云端 cancel 收口）。
                if budget:
                    await asyncio.wait_for(asyncio.shield(build), timeout=budget)
                else:
                    await asyncio.shield(build)
            finally:
                if ticker is not None:
                    ticker.cancel()  # 生产终止即停发（终态时刻，不等 finally——评审 M1）
            await self._send_chunks(ws, req_id, call_id, Path(tmp_name))
        except asyncio.TimeoutError:
            if not build.done():
                # 预算超时（wait_for 触发）：先回契约 timeout 码（fail fast），再排空线程
                await self._reply_result(req_id, call_id, {"ok": False, "error": (
                    f"timeout: fs/zip build exceeded {budget:.0f}s")}, ws=ws)
                with contextlib.suppress(Exception):
                    await asyncio.wait({build}, timeout=30)
            else:
                # build 自身抛 TimeoutError（Py3.13: asyncio.TimeoutError is TimeoutError）——
                # 按打包失败处理，不冒充预算超时（评审 M2）
                logger.warning("fs/zip build raised TimeoutError", exc_info=True)
                await self._reply_result(req_id, call_id, {"ok": False, "error": (
                    "internal_error: fs/zip build failed")}, ws=ws)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.wait({build}, timeout=30)
            raise
        except OSError as exc:
            # 与 files.py /zip 同源：OSError（源读/临时写）→统一 not-found 面，
            # 固定串不回显路径（复评 I-1 同类腿），细节进服务端日志。
            logger.warning("fs/zip build failed: %s", exc)
            await self._reply_result(req_id, call_id, {
                "ok": False, "error": "workspace_path_not_found: file not found"}, ws=ws)
        finally:
            if ticker is not None:
                ticker.cancel()
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)

    async def _send_office_converted(self, ws: Any, req_id: Any, call_id: Any,
                                     target: Path, config: Any, kind: str) -> None:
        """fs/office-html|fs/office-pdf（W19）：设备侧 soffice 转换→同形字节通道。

        转换链路与 `/api/files/office-*` 共用 `convert_workspace_document`
        （TOCTOU 冻结 + 缓存 + spool 清理）；错误面与本地端点同源：
        `office_preview_unavailable`（501）/ `office_preview_failed`（400/422）。
        """
        from app.services.office_preview_service import (
            OfficePreviewFailed,
            OfficePreviewPreparationError,
            OfficePreviewUnavailable,
            convert_workspace_document,
            ensure_soffice,
            is_spreadsheet,
            is_supported,
        )

        async def fail(error: str) -> None:
            await self._reply_result(req_id, call_id, {"ok": False, "error": error}, ws=ws)

        if not target.is_file():
            await fail("workspace_path_not_found: file not found")
            return
        if kind == "html":
            if not is_spreadsheet(target.name):
                await fail("office_preview_failed: not a spreadsheet file")
                return
        elif not is_supported(target.name):
            await fail("office_preview_failed: unsupported office file type")
            return
        if not config.office_preview_enabled or (
                kind == "html" and not config.office_preview_html_enabled):
            await fail("office_preview_unavailable: office preview conversion is disabled")
            return
        soffice = await ensure_soffice(config.office_preview_soffice_path)
        if not soffice:
            await fail("office_preview_unavailable: LibreOffice is not installed")
            return
        ticker = None
        budget = float(get_config().client_exec_call_max_extension_seconds or 0.0)
        # RPC 路径转换超时=续期硬上限镜像（长文档可被云端续期等待）；0=不限制时回退
        # office_preview_timeout_seconds 兜底（soffice 须有限超时=进程组 kill 边界——
        # D-94：0 的「不限制」不延伸到子进程；长文档请设正预算或上调 office 钮）。
        convert_timeout = (
            max(1, int(budget)) if budget > 0 else int(config.office_preview_timeout_seconds))
        started_at = time.monotonic()
        if req_id is not None:
            ticker = asyncio.ensure_future(self._progress_ticker(
                ws, str(req_id),
                lambda: f"{kind}-convert {time.monotonic() - started_at:.0f}s",
                float(get_config().client_exec_call_timeout_seconds)))
            ticker.add_done_callback(_swallow_late)
        try:
            try:
                produced = await convert_workspace_document(
                    target,
                    kind=kind,
                    soffice=soffice,
                    cache_dir=config.office_preview_cache_dir,
                    timeout=convert_timeout,
                    max_cache_mb=config.office_preview_cache_max_mb,
                )
            finally:
                if ticker is not None:
                    ticker.cancel()  # 生产终止即停发（终态时刻——评审 M1）
        except OSError as exc:
            # 源不可达/非常规文件（chmod 000/竞态换链）——与 files.py 统一 404 语义：
            # 固定串不回显 OS 路径（复评 I-1 残留腿），细节只进服务端日志。
            logger.info("fs/%s source open failed for %r: %s", kind, target.name, exc)
            await fail("workspace_path_not_found: file not found")
            return
        except OfficePreviewUnavailable:
            logger.warning("fs/%s soffice unavailable", kind, exc_info=True)
            await fail("office_preview_unavailable: LibreOffice is not installed")
            return
        except OfficePreviewFailed:
            logger.warning("fs/%s conversion failed", kind, exc_info=True)
            await fail("office_preview_failed: office conversion failed or timed out")
            return
        except OfficePreviewPreparationError:
            # 内部 I/O 故障（缓存写/冻结）：固定串回云端——不回显 OS 路径（评审 I-1）
            logger.warning("fs/%s preparation failed", kind, exc_info=True)
            await fail("internal_error: preview preparation failed")
            return
        finally:
            if ticker is not None:
                ticker.cancel()
        await self._send_chunks(ws, req_id, call_id, produced)

    async def _send_chunks(self, ws: Any, req_id: Any, call_id: Any, target: Path) -> None:
        """W16b/W19 chunk 契约：**result 先行**（D-91），chunk 流后至走云端汇编窗。

        result = {ok, data_b64, sha256}（≤CHUNK_SIZE 单包，空文件 data_b64:""）或
        {ok, transfer_id, sha256, size, chunks}（分块）；chunk = {transfer_id, seq,
        data_b64, sha256(本块), last:true(末块)}（逐块 sha256 校验）。

        发序=上游时序模型（device_session_service：rpc_call 等 result，`_assemble_transfer`
        自 result 后独立计时窗）：汇总必须先于首块，上传全程落在汇编窗内而非 RPC 等待窗。
        分块路径**同一 fd 两遍**：先通读预哈希（定 size/sha256/chunks）→发汇总→seek(0)
        恰流 size 字节——同 fd=同一 inode，路径被替换也不吐错字节（TOCTOU 不回潮）；
        源被并发截断/中途读失败时补空末块 last:true 立即收束（部分截断→摘要不符由云端
        fail-closed 拒收；截断到空→按读取现况交付空文件，摘要自洽）。
        """
        with target.open("rb") as f:
            head = f.read(CHUNK_SIZE + 1)  # 首读实况定路（评审 I2：stat 前置读 TOCTOU）
            if len(head) <= CHUNK_SIZE:
                await self._reply_result(req_id, call_id, {
                    "ok": True,
                    "data_b64": base64.b64encode(head).decode("ascii"),
                    "sha256": hashlib.sha256(head).hexdigest(),
                }, ws=ws)
                return

            cap_mb = int(get_config().client_exec_max_payload_mb)
            cap_bytes = cap_mb * 1024 * 1024
            f.seek(0)
            total = hashlib.sha256()
            size = 0
            while True:
                block = f.read(min(1024 * 1024, cap_bytes + 1 - size))
                if not block:
                    break
                total.update(block)
                size += len(block)
                if size > cap_bytes:
                    break  # 超限早期退出（评审 A-I2：不为注定拒绝的巨物通读全文件）
                await asyncio.sleep(0)  # 让出事件循环：慢盘长读期间 ticker 仍能发（A-I2）

            # 补波 2 oversize 预检：预哈希实测 size 对闸，超限在发任何字节/汇总前拒
            # （云端汇编同闸 413，这里提前明确拒绝，不流注定失败的大包）。
            # 单包路径免检：head ≤CHUNK_SIZE(256KB) < max_payload_mb 下限(1MB)。
            # 消息含「超过」=上游 remote_dispatch_router 的 oversize 分类词（评审 I-1：
            # 消息须与云端既有「文件超过设备代理上限」同词表，供其413/文案面识别）。
            cap_mb = int(get_config().client_exec_max_payload_mb)
            if size > cap_bytes:
                await self._reply_result(req_id, call_id, {"ok": False, "error": (
                    f"payload_too_large: 文件超过设备代理上限（max_payload_mb {cap_mb}）")}, ws=ws)
                return

            transfer_id = uuid.uuid4().hex
            await self._reply_result(req_id, call_id, {
                "ok": True,
                "transfer_id": transfer_id,
                "sha256": total.hexdigest(),
                "size": size,
                "chunks": (size + CHUNK_SIZE - 1) // CHUNK_SIZE,
            }, ws=ws)

            f.seek(0)
            seq = 0
            remaining = size
            sent_last = False

            def _emit(chunk: bytes, last: bool, n: int) -> dict:
                return {
                    "transfer_id": transfer_id,
                    "seq": n,
                    "data_b64": base64.b64encode(chunk).decode("ascii"),
                    "sha256": hashlib.sha256(chunk).hexdigest(),
                    "last": last,
                }

            try:
                while remaining > 0:
                    piece = f.read(min(CHUNK_SIZE, remaining))
                    if not piece:
                        break  # 源被并发截断 → 下方兜底终止
                    remaining -= len(piece)
                    sent_last = remaining == 0
                    await self._send({"method": "chunk", "params": _emit(piece, sent_last, seq)}, ws=ws)
                    seq += 1
            except OSError:
                logger.warning("fs/* chunk stream failed", exc_info=True)
            if not sent_last:
                # 兜底终止帧（截断/中途读失败）：空末块让对端立即收束汇编，绝不干等汇编窗超时。
                # 部分截断→重组≠汇总摘要→云端 fail-closed 拒收；截断到空→按读取现况交付空
                # 文件（摘要自洽，与「首读实况定路」同哲学）。两路都至少即时终止。
                with contextlib.suppress(Exception):
                    await self._send({"method": "chunk", "params": _emit(b"", True, seq)}, ws=ws)

    def _remember_call(self, call_id: str) -> None:
        if call_id in self._done_calls:
            return
        self._done_calls.add(call_id)
        self._done_order.append(call_id)
        while len(self._done_order) > 256:  # FIFO 裁剪（评审 minor：set 裁剪无序）
            self._done_calls.discard(self._done_order.pop(0))

    async def _reply_call(self, entry: Dict[str, Any], call_id: str, *, ok: bool,
                          payload: Any = None, code: str = "", message: str = "") -> None:
        """call 级应答唯一出口（W16b 形状：{ok,content} / {ok:false,error}；replied 互斥）。"""
        if entry.get("replied"):
            return
        entry["replied"] = True
        if ok:
            body = {"ok": True, "content": payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)}
        else:
            body = {"ok": False, "error": f"{code}: {message}" if code else message}
        await self._reply_result(entry["id"], call_id, body)

    # ---------- 应答 ----------
    async def _send(self, msg: Dict[str, Any], ws: Any = None) -> None:
        ws = ws if ws is not None else self._ws
        if ws is None:
            return
        msg.setdefault("jsonrpc", "2.0")
        try:
            await ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception:
            logger.debug("device executor send failed", exc_info=True)

    async def _reply_result(self, req_id: Any, call_id: Any, payload: Any, ws: Any = None) -> None:
        """result 体=载荷本体（fs/initialize 透传字典；工具面由 _reply_call 构造 ok/content）。"""
        await self._send({"id": req_id, "result": payload}, ws=ws)

    async def _reply_error(self, req_id: Any, call_id: Any, code: str, message: str, ws: Any = None) -> None:
        """协议级失败信封（上游读 error.message → DeviceCallError）。"""
        await self._send({
            "id": req_id,
            "error": {"call_id": call_id, "code": code, "message": message},
        }, ws=ws)

    # ---------- 身份/工作区绑定（W20 显式绑定链，PLAN_device_workspace_identity_impl T3）----------
    async def _resolve_identity(self, binding: Optional[Dict[str, Any]] = None) -> Tuple[Any, str, Optional[str]]:
        """显式绑定链：配对 local_user_id → sync 绑定（cloud_username 匹配）→ fail-closed
        （identity_unbound；绝不回落「首个用户」——workspace_mismatch 事故根因）。
        binding=调用发起时快照（防在途换绑偷换身份，评审 r2-F7）；None=自行读取。
        返回 (owner, ws_root, expected_ctx)；expected_ctx=名门期望 workspace_ctx
        （云端身份导出 {slug(cloud_username)}-{cloud_user_id[:8]}；None=无云端身份）。"""
        from app.db.database import AsyncSessionLocal
        from app.services.workspace_service import ensure_user_workspace

        binding = binding if binding is not None else (load_binding() or {})
        async with AsyncSessionLocal() as db:
            owner, expected = await resolve_execution_identity(binding, db)
            ws = await ensure_user_workspace(db, owner.id, getattr(owner, "username", None))
            root = Path(ws.root_path).resolve()
            return owner, str(root), expected


def expected_workspace_ctx(cloud_username: str, cloud_user_id: str) -> Optional[str]:
    """名门期望值公式（Hub 单一真源）：{slug(cloud_username)}-{cloud_user_id[:8]}——
    与云端 remote_dispatch_router._workspace_ctx 同式同源。"""
    if cloud_username and cloud_user_id:
        from app.services.workspace_service import _slugify

        return f"{_slugify(cloud_username)}-{str(cloud_user_id)[:8]}"
    return None


class IdentityUnboundError(RuntimeError):
    """设备未绑定执行身份（fail-closed）。wire code=identity_unbound（不得降级）。"""


async def resolve_execution_identity(binding: Dict[str, Any], db: Any) -> Tuple[Any, Optional[str]]:
    """显式绑定链解析（W20）：返回 (owner, expected_workspace_ctx)。

    优先级（PLAN_device_workspace_identity_fix §2 裁决）：配对记录 local_user_id →
    sync 绑定（cloud_username 匹配）→ fail-closed RuntimeError("identity_unbound: …")。
    expected=云端身份导出 `{slug(cloud_username)}-{cloud_user_id[:8]}`（云端
    remote_dispatch_router._workspace_ctx 同式同源；跨栈 uuid 不参与）；无云端身份 → None
    （名门侧 fail-closed）。"""
    from sqlalchemy import select

    from app.db.database import SyncState, User

    binding = binding or {}
    local_user_id = str(binding.get("local_user_id") or "")
    b_cloud_user = str(binding.get("cloud_user_id") or "")
    b_cloud_name = str(binding.get("cloud_username") or "")

    _expected = expected_workspace_ctx

    expected = _expected(b_cloud_name, b_cloud_user)
    owner = None
    if local_user_id:
        owner = await db.get(User, local_user_id)
    if owner is None and b_cloud_name:
        res = await db.execute(
            select(SyncState)
            .where(SyncState.cloud_username == b_cloud_name)
            .order_by(SyncState.user_id)  # 多行同名确定序（评审 r2-F9/r1-F4）
        )
        st = res.scalars().first()
        if st is not None:
            owner = await db.get(User, str(st.user_id))
            if expected is None:
                expected = _expected(str(getattr(st, "cloud_username", "") or ""),
                                     str(getattr(st, "cloud_user_id", "") or ""))
    if owner is None:
        raise IdentityUnboundError("设备未绑定执行身份——请重新配对（或重新提交同步绑定）")
    if expected is None:
        st = await db.get(SyncState, str(getattr(owner, "id", "") or ""))
        if st is not None:
            expected = _expected(str(getattr(st, "cloud_username", "") or ""),
                                 str(getattr(st, "cloud_user_id", "") or ""))
    return owner, expected


device_executor = DeviceExecutorWorker()
