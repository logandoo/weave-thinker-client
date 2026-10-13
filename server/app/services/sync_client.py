"""客户端同步 worker（S2 波 → 2026-09-26 per-user 绑定波重构）：本地栈 ↔ 远端同步服务器。

绑定模型（per-user）：任意登录用户经 PUT /api/sync/account 自助绑定云端账号——
sync_state 行 = 「本用户 binding（server_url/云账号/密码/enabled）+ 游标
（push_cursor/pull_cursor）+ device_id」。worker 每周期迭代全部 enabled 绑定，
逐账号 push/pull/blobs（本地 sync_events 即出站队列，rx: 前缀防回显环）。
[sync.client] 旧单账号配置仅作一次性导入（启动时落为对应用户的绑定行后退役）。

算法不变（Joplin 模式，见 S2/S3 注释与 API.md 二.13）：LWW 双端同源
（sync_apply.apply_change）· per-event SAVEPOINT · 冲突记 sync_conflicts ·
断网周期容错记绑定行 last_error · blob 上行 diff + 懒穿透续传。
"""
import asyncio
import hashlib
import json
import logging
import os
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_config
from app.db.database import (
    AsyncSessionLocal,
    SyncConflict,
    SyncEvent,
    SyncState,
    User,
)
from app.services.sync_apply import APPLIED, STALE, apply_change, parse_dt
from app.services.sync_capture import reset_origin_device, set_origin_device

logger = logging.getLogger(__name__)


class SyncClient:
    _PUSH_BATCH_CAP = 10  # 单周期单账号推送批数上限（防 pull 饥饿）

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event = asyncio.Event()  # resync/sync-now 立即唤醒周期（判据#4/#5）
        self._resync_pending: set = set()  # 请求全量 resync 的 user_id（A4.9 评审 B#2：完成因果锚，防在途周期假满足）
        self._last_kick: dict = {}         # user_id → 最近 wake 时刻（monotonic；判据#8 限频）
        self._tokens: dict = {}            # user_id → 云端 JWT（内存缓存，401 重登）
        self._devices_registered: set = set()  # user_id → 本进程已注册设备
        self._blob_fail: dict = {}   # (user_id, sha256) → 进程内连续失败计数（退避）
        self._neg_cache: dict = {}   # (user_id, sha256) → 负缓存到期时刻（远端 404 降频）
        self._memory_touched: set = set()  # 本周期 pull 应用过记忆行的 user_id
        self._workspace_pending: set = set()  # workspace 小文件未拉完的 user_id（跨周期续拉）
        self._workspace_fail: dict = {}  # (user_id, rel_path) → 连续失败计数（≥3 退避）
        self._workspace_swept: dict = {}  # user_id → 上次 .syncpart 清扫的 loop 时刻（节流）
        self._legacy_imported = False

    async def start(self) -> None:
        cfg = get_config()
        if not cfg.sync_enabled:
            logger.info("SyncClient disabled（[sync] enabled=false）")
            return
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="sync-client")
            logger.info(
                "SyncClient started (per-user bindings; interval=%ds, batch=%d)",
                cfg.sync_client_interval, cfg.sync_client_batch,
            )

    def wake(self) -> None:
        """立即唤醒周期（resync/sync-now 用）：不等 interval tick，下一轮马上跑。"""
        self._wake.set()

    def allow_kick(self, user_id: str, cooldown_s: float = 5.0) -> bool:
        """per-user 唤醒限频（判据#8，A4.9 A#11/B#10）：冷却内重复请求拒绝——
        防任意登录用户刷 sync-now/resync 反复强制 _cycle_all 放大远端流量。
        返回 True=记账并放行；False=冷却内。"""
        import time as _time
        now = _time.monotonic()
        last = self._last_kick.get(user_id)
        if last is not None and (now - last) < cooldown_s:
            return False
        self._last_kick[user_id] = now
        return True

    def request_resync(self, user_id: str) -> None:
        """登记全量 resync 请求（A4.9 评审 B#2）：在途周期的周期尾盖章无法再吞掉该请求
        ——下一个周期经 _resync_pending 消费必跑 _resync；仅成功跑完才清除标记。"""
        self._resync_pending.add(user_id)

    def is_resync_pending(self, user_id: str) -> bool:
        """供 /sync/status 暴露：前端据此把「全量完成」锚定到真正跑完 resync 的周期。"""
        return user_id in self._resync_pending

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # ── 主循环：迭代全部 enabled 绑定 ──────────────────────────
    async def _run(self) -> None:
        while True:
            try:
                await self._cycle_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 顶层兜底（正常不应到这）
                logger.warning("SyncClient cycle_all failed: %s: %s", type(exc).__name__, exc)
            # wake 优先（判据#4/#5）：Event 唤醒立即跑下一轮；否则按 interval 超时兜底
            try:
                await asyncio.wait_for(self._wake.wait(),
                                       timeout=get_config().sync_client_interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _cycle_all(self) -> None:
        async with AsyncSessionLocal() as db:
            await self._import_legacy_config(db)
            bindings = (
                await db.execute(
                    select(SyncState).where(
                        SyncState.sync_enabled.is_(True),
                        SyncState.server_url.is_not(None),
                        SyncState.cloud_username.is_not(None),
                        SyncState.cloud_password.is_not(None),
                    )
                )
            ).scalars().all()
            # M4：剪枝过期缓存（解绑/换绑后不再命中的条目有界化）
            live_keys = {self._tkey(st) for st in bindings}
            self._tokens = {k: v for k, v in self._tokens.items() if k in live_keys}
            self._devices_registered &= live_keys
            # blob 退避/负缓存按 device_id 剪枝（换绑换 device_id → 新账号重新计数）
            live_devices = {st.device_id for st in bindings}
            self._blob_fail = {k: v for k, v in self._blob_fail.items() if k[0] in live_devices}
            self._neg_cache = {k: v for k, v in self._neg_cache.items() if k[0] in live_devices}
            live_users = {st.user_id for st in bindings}
            self._workspace_pending &= live_users
            self._workspace_fail = {k: v for k, v in self._workspace_fail.items()
                                    if k[0] in live_users}
            self._workspace_swept = {k: v for k, v in self._workspace_swept.items()
                                     if k in live_users}
            # I1（评审+实测 MissingGreenlet 根因修正）：共享会话里一次 rollback
            # 会使全部已加载绑定行过期，下一绑定访问属性即懒刷新 MissingGreenlet——
            # 改为每绑定独立会话（最强隔离；user_id 在会话外先取为纯值）。
            for uid in [st.user_id for st in bindings]:
                async with AsyncSessionLocal() as db_b:
                    state = await db_b.get(SyncState, uid)
                    user = await db_b.get(User, uid)
                    if state is None or user is None:
                        continue
                    try:
                        await self._cycle_one(db_b, user, state)
                    except Exception as exc:  # noqa: BLE001 — 单账号失败不连坐其它账号
                        logger.warning(
                            "SyncClient cycle failed for user %s: %s: %s",
                            uid[:8], type(exc).__name__, exc,
                        )
                        await db_b.rollback()
                        fresh = await db_b.get(SyncState, uid)
                        if fresh is not None:
                            fresh.last_error = f"{type(exc).__name__}: {exc}"[:1000]
                            await db_b.commit()

    async def _import_legacy_config(self, db) -> None:
        """[sync.client] 旧单账号配置 → 一次性落为绑定行（进程内一次）。"""
        if self._legacy_imported:
            return
        cfg = get_config()
        if not (cfg.sync_client_enabled and cfg.sync_client_server_url and cfg.sync_client_username):
            self._legacy_imported = True
            return
        user = (
            await db.execute(select(User).where(User.username == cfg.sync_client_username))
        ).scalar_one_or_none()
        if user is None:
            # M1：用户尚未创建——保持未导入态，下周期重试
            logger.info("sync legacy import: 本地用户尚未存在，待重试 %s", cfg.sync_client_username)
            return
        existing = await db.get(SyncState, user.id)
        if existing is not None and existing.server_url:
            return  # 已有绑定，不覆盖
        row = existing or SyncState(user_id=user.id, device_id=uuid.uuid4().hex)
        row.server_url = cfg.sync_client_server_url
        row.cloud_username = cfg.sync_client_username
        row.cloud_password = cfg.sync_client_password
        row.sync_enabled = True
        db.add(row)
        await db.commit()
        self._legacy_imported = True
        logger.info("sync legacy [sync.client] 已导入为 %s 的绑定行", cfg.sync_client_username)

    # ── 单账号周期 ────────────────────────────────────────────
    async def _cycle_one(self, db, user, state) -> None:
        async with self._client(state.server_url, state.tls_verify if state.tls_verify is not None else True) as client:
            if self._tkey(state) not in self._devices_registered:
                await self._call(
                    state, client, "POST", "/api/sync/devices",
                    json={"id": state.device_id, "name": "local-stack", "platform": "desktop"},
                )
                self._devices_registered.add(self._tkey(state))
            if (user.id in self._resync_pending
                    or (state.last_sync_at is None and (state.pull_cursor or 0) == 0)):
                # 首次同步先 resync 全量基线（实测缺口：sync_events 只含部署后变更，
                # 存量数据必须经 resync 快照拉取——Joplin 首连同款）。
                # _resync_pending=显式请求锚（A4.9 评审 B#2）：请求后必跑，不被在途
                # 周期尾盖章吞掉；仅成功跑完清除标记（异常保留→下周期重试）。
                await self._resync(db, client, user, state)
                self._resync_pending.discard(user.id)
            await self._push(db, client, user, state)
            pull_errors = await self._pull(db, client, user, state)
            await self._blobs_push(db, client, user, state)
            # workspace 小文件续拉（上次全量同步未拉完的部分；每周期有界）
            if user.id in self._workspace_pending:
                try:
                    await self.sync_workspace_files(client, user.id, user.username, state)
                except Exception as exc:  # noqa: BLE001 — 续拉失败下周期再试（pending 保留）
                    logger.warning("sync workspace continuation failed (user=%s): %s",
                                   user.id[:8], exc)

        state.last_sync_at = datetime.utcnow()
        # apply 错误留痕由 _pull 返回值带出（不得在周期尾无条件清掉）
        state.last_error = f"{pull_errors} pull event(s) failed to apply" if pull_errors else None
        await db.commit()

    def _client(self, server_url: str, tls_verify: bool = True) -> httpx.AsyncClient:
        # trust_env=False：macOS 系统代理会被 httpx 读取，远端停机时拿到代理
        # 502 而非 ConnectError，破坏失败语义（local_stack_auth 同款教训）
        # W9 尺寸闸（用户指令）：读/写超时抬高承载大事件（全文解析后 message
        # 事件可达 MB 级）；连接/池保持短超时防坏端点拖拽。
        return httpx.AsyncClient(base_url=server_url.rstrip("/"), trust_env=False,
                                 timeout=httpx.Timeout(connect=10.0, read=60.0,
                                                       write=60.0, pool=15.0),
                                 verify=tls_verify)

    def _tkey(self, state) -> tuple:
        # 缓存身份键（C1）：换绑（server/账号/device 变更）后旧条目自然失效
        return (state.user_id, state.server_url, state.cloud_username, state.device_id)

    async def _login(self, state, client: httpx.AsyncClient) -> None:
        try:
            r = await client.post(
                "/api/auth/login",
                json={"username": state.cloud_username, "password": state.cloud_password},
            )
            r.raise_for_status()
        except Exception:
            self._tokens.pop(self._tkey(state), None)  # 登录失败不留陈 token
            self._devices_registered.discard(self._tkey(state))
            raise
        self._tokens[self._tkey(state)] = r.json()["access_token"]
        self._devices_registered.discard(self._tkey(state))
        # W20 惰性回填：存量绑定缺 cloud_user_id → 设备名门期望值不可算（identity_unbound）。
        # 顺手取 /api/auth/me（有 token 单次），成功即停（行已非空则不再写/不再请求）。
        if not getattr(state, "cloud_user_id", ""):
            # 串行 await（评审修正：ensure_future 游离任务在测试循环关闭时污染真 DB 连接）；
            # 登录本就低频，单次 GET 代价可接受，确定性 > 并行度
            await self._backfill_cloud_identity(state, client)

    async def _backfill_cloud_identity(self, state, client: httpx.AsyncClient) -> None:
        """best-effort 回填 SyncState.cloud_user_id（设备名门期望值来源）。
        重试节奏=重新登录触发（token 轮换/401），非每周期（复评 NEW-4 勘误）。"""
        try:
            token = self._tokens.get(self._tkey(state), "")
            if not token:
                return
            r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 200:
                return
            uid = str((r.json() or {}).get("id") or "")
            if not uid:
                return
            from app.db.database import AsyncSessionLocal, SyncState

            async with AsyncSessionLocal() as db:
                row = await db.get(SyncState, state.user_id)
                if row is not None and not row.cloud_user_id:
                    row.cloud_user_id = uid[:64]
                    await db.commit()
                    logger.info("sync identity backfilled (user=%s)", state.user_id)
        except Exception:
            logger.debug("sync identity backfill failed", exc_info=True)

    async def _call(self, state, client: httpx.AsyncClient, method: str, path: str, **kw) -> httpx.Response:
        # 调用方可经 headers= 追加（如 Range）——与 Authorization 合并而非冲突
        extra = dict(kw.pop("headers", None) or {})
        token = self._tokens.get(self._tkey(state))
        if not token:
            await self._login(state, client)
            token = self._tokens[self._tkey(state)]
        headers = {"Authorization": f"Bearer {token}", **extra}
        r = await client.request(method, path, headers=headers, **kw)
        if r.status_code == 401:  # token 过期：重登一次重试
            await self._login(state, client)
            headers = {"Authorization": f"Bearer {self._tokens[self._tkey(state)]}", **extra}
            r = await client.request(method, path, headers=headers, **kw)
        r.raise_for_status()
        return r

    # ── 首连全量基线（resync 快照 → upsert 应用 → pull_cursor 复位）────────
    async def _clear_queued_events(self, db, user, tombstone: dict) -> None:
        """墓碑删除后清本地待推队列（W10-2 评审 I2；自然键命中行一并清）。

        与 _merge_default_assistant 同款「删除权威」模式：本地待推 create/update
        会把已删实体复活到服务端；而本端（origin=device_id）因回显抑制再也拉不回
        自己的事件 → 本地/服务端静默分叉直至手动全量。清队列使删除在两端收敛。
        """
        entity_type = str(tombstone.get("entity_type", ""))
        eids = {str(tombstone.get("entity_id", ""))} - {""}
        pl = tombstone.get("payload")
        if entity_type == "user_model_providers" and isinstance(pl, dict):
            from app.db.database import UserModelProvider as _UMP

            row = (await db.execute(
                select(_UMP.id).where(
                    _UMP.user_id == user.id,
                    _UMP.kind == pl.get("kind", ""),
                    _UMP.provider == pl.get("provider", ""),
                )
            )).first()
            if row and row[0]:
                eids.add(str(row[0]))
        for eid in eids:
            await db.execute(
                delete(SyncEvent).where(
                    SyncEvent.user_id == user.id,
                    SyncEvent.entity_type == entity_type,
                    SyncEvent.entity_id == eid,
                    ~func.coalesce(SyncEvent.origin_device, "").like("rx:%"),
                )
            )

    async def _resync(self, db, client, user, state) -> None:
        r = await self._call(state, client, "POST", "/api/sync/resync")
        body = r.json()
        applied = 0
        # 依赖序应用（实测缺口：entities 字典序 conversations→…→assistants，
        # 子实体先于父实体应用必被 parents_owned 全灭——13 notes/20 convs 实证）
        _ORDER = ("assistants", "user_skills", "conversation_groups", "notebooks",
                  "user_asr_hotwords", "user_model_providers", "conversations",
                  "notes", "skill_files", "messages",
                  "memory_episodes", "memory_concepts", "user_profile")
        entities = body.get("entities") or {}
        # 8b 客户端契约（W9，用户指令）：先墓碑后快照——本地仍存在的已删行先
        # 清掉再并入快照（上游 resync 序：墓碑查询先于快照同点交付；服务端契约
        # 注释「客户端契约=先应用墓碑再并快照」）。单条失败隔离，不阻断基线。
        tbs = body.get("tombstones") or []
        if tbs:
            t_deleted = 0
            for t in tbs:
                # 健壮（评审 M1）：非 dict 项（脏载荷/未来形态）跳过并告警——
                # 异常处理器自身不得再解引用毒项（旧形会在 handler 内二次抛错，
                # 令整次 resync 中止）。
                if not isinstance(t, dict):
                    logger.warning("resync tombstone entry not a dict — skipped: %r", t)
                    continue
                try:
                    async with db.begin_nested():
                        origin_ctx = set_origin_device("rx:resync")
                        try:
                            # W10（用户指令）：墓碑载荷必须转发（上游 W9 项9 墓碑
                            # payload 列=自然键 {kind,provider}）——apply_change
                            # 自然键兜底依赖它，否则异 id 归并行（user_model_providers
                            # 合并场景）永远删不掉（评审 I2 复活面）。空载荷契约=
                            # 无该键 → get 得 None，行为与旧版一致。
                            res = await apply_change(
                                db, user, str(t.get("entity_type", "")),
                                str(t.get("entity_id", "")), "delete",
                                t.get("payload"),
                            )
                        finally:
                            reset_origin_device(origin_ctx)
                    if res == APPLIED:
                        t_deleted += 1
                    # W10-2（评审 I2）：删除权威 + 清本地待推队列——自然键兜底
                    # 可能删掉与墓碑不同 id 的同自然键本地行（删后重建/编辑的
                    # 新行）；其待推 create/update 会把删除复活到服务端，而本端
                    # 因回显抑制永远拉不回自己的事件 → 静默分叉。与
                    # _merge_default_assistant 同款：resync 删除后清该实体
                    # （含自然键命中的本地行）的待推队列。
                    await self._clear_queued_events(db, user, t)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "resync tombstone failed (%s %s): %s",
                        t.get("entity_type"), t.get("entity_id"), exc,
                    )
            if t_deleted:
                logger.info("sync resync tombstones: deleted=%d (user=%s)",
                            t_deleted, user.id[:8])
        # 模型池配置（2026-09-29 波，D-21）：服务端下行 → 白名单落盘 config_model.toml
        # + 热重载注册表。辅助腿独立 try/except——配置落盘失败不阻断数据基线。
        try:
            mc_rows = entities.get("model_config")
            if isinstance(mc_rows, list) and mc_rows and isinstance(mc_rows[0], dict):
                from app.model_gateway.registry import reload_model_registry
                from app.services.model_config_sync import (
                    apply_model_config,
                    resolve_model_config_target,
                )

                if apply_model_config(
                    mc_rows[0], resolve_model_config_target(),
                    source=getattr(state, "server_url", None),
                ):
                    reg = reload_model_registry()
                    logger.info(
                        "sync model_config applied: llm aliases=%s",
                        [ep.get("alias") for ep in reg.public_aliases(kind="llm")],
                    )
        except Exception as exc:  # noqa: BLE001 — 配置辅助腿不阻断基线
            logger.warning("sync model_config apply failed: %s", exc)
        ordered = [(et, entities[et]) for et in _ORDER if et in entities]
        ordered += [(et, rows) for et, rows in entities.items()
                    if et not in _ORDER and et != "model_config"]
        for entity_type, rows in ordered:
            if entity_type == "assistants":
                await self._merge_default_assistant(db, user)
            for row in rows:
                try:
                    async with db.begin_nested():
                        origin_ctx = set_origin_device("rx:resync")
                        try:
                            # C1（评审）：user_profile 无 create——resync 基线按 update 应用
                            op = "update" if entity_type == "user_profile" else "create"
                            result = await apply_change(
                                db, user, entity_type, row.get("id"), op, row
                            )
                        finally:
                            reset_origin_device(origin_ctx)
                        if result == APPLIED:
                            applied += 1
                        if entity_type in ("memory_episodes", "memory_concepts") and result == APPLIED:
                            self._memory_touched.add(user.id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("resync row failed (%s %s): %s", entity_type, row.get("id"), exc)
        state.pull_cursor = body.get("cursor", 0)
        await db.commit()
        logger.info("sync resync baseline: applied=%d cursor=%s (user=%s)",
                    applied, state.pull_cursor, user.id[:8])
        # workspace 文件物化（2026-09-29 波）：全量基线后把云端 workspace 的
        # 小文件自动拉到本地（大文件走预览按需）。失败不阻断实体基线。
        if get_config().sync_workspace_enabled:
            try:
                ws_result = await self.sync_workspace_files(
                    client, user.id, user.username, state
                )
                logger.info("sync workspace pull: %s", ws_result)
            except Exception as exc:  # noqa: BLE001 — 文件物化失败不连坐实体同步
                logger.warning("sync workspace pull failed: %s: %s",
                               type(exc).__name__, exc)

    async def _merge_default_assistant(self, db, user) -> None:
        """D-17 + 评审 C2：仅「本地创建且从未同步过」的空默认助手才合并
        （云端同步来的默认助手其 create 事件 origin=rx: → 不二次合并——
        否则手动重 resync 会把云端默认助手删掉并上行墓碑）。"""
        from app.core.config import get_config as _gc
        from app.db.database import Assistant, Conversation, SyncEvent

        rows = (
            await db.execute(select(Assistant).where(Assistant.user_id == user.id))
        ).scalars().all()
        if len(rows) != 1:
            return
        local = rows[0]
        if local.name != _gc().default_assistant_name:
            return
        refs = (
            await db.execute(
                select(func.count(Conversation.id)).where(
                    Conversation.assistant_id == local.id
                )
            )
        ).scalar() or 0
        if refs:
            logger.info("sync default-assistant merge skipped (local has %d refs)", refs)
            return
        # C2 门控：该默认助手若已是云端同步来的（create 事件 origin=rx:）→ 不二次合并
        origin_row = (
            await db.execute(
                select(SyncEvent.origin_device).where(
                    SyncEvent.entity_type == "assistants",
                    SyncEvent.entity_id == local.id,
                    SyncEvent.op == "create",
                )
            )
        ).first()
        if origin_row is None or origin_row[0] is not None:
            logger.info("sync default-assistant merge skipped (not a fresh local default)")
            return
        # 清未推 create/update 事件（本地刚注册，事件还在出站队列里）；
        # 删除本体包 rx:resync origin——删除事件不进上行队列（评审 C2：防
        # 云端默认助手被误删并广播墓碑）
        origin_ctx = set_origin_device("rx:resync")
        try:
            await db.execute(
                SyncEvent.__table__.delete().where(
                    SyncEvent.user_id == user.id,
                    SyncEvent.entity_type == "assistants",
                    SyncEvent.entity_id == local.id,
                )
            )
            await db.delete(local)
            await db.flush()
        finally:
            reset_origin_device(origin_ctx)
        logger.info("sync default-assistant merge: local empty default replaced by cloud (user=%s)",
                    user.id[:8])

    # ── 出站（本地 sync_events → 远端 push）─────────────────────
    async def _push(self, db, client, user, state) -> None:
        batch = get_config().sync_client_batch
        for _ in range(self._PUSH_BATCH_CAP):
            # rx: 过滤必须 NULL 安全（SQL 三值逻辑：NULL LIKE x → NULL → 行被排除，
            # 会把 origin=NULL 的本地 UI 变更全吞掉——iter 3 根因）
            rows = (
                await db.execute(
                    select(SyncEvent)
                    .where(
                        SyncEvent.user_id == user.id,
                        SyncEvent.seq > (state.push_cursor or 0),
                        ~func.coalesce(SyncEvent.origin_device, "").like("rx:%"),
                    )
                    .order_by(SyncEvent.seq)
                    .limit(batch)
                )
            ).scalars().all()
            if not rows:
                return
            items = [
                {
                    "entity_type": e.entity_type,
                    "entity_id": e.entity_id,
                    "op": e.op,
                    "payload": e.payload,
                }
                for e in rows
            ]
            # W9 尺寸闸（用户指令）：按事件体字节预算分块——单请求 ≤ 预算
            # （预留包体壳 128B）；单事件超预算独占一块（不拆事件：截断=数据
            # 丢失被禁）。逐块发送/提交/推进游标：块失败上抛仅该块原位重试，
            # 故障半径有界（不再整窗头阻塞）。
            max_bytes = get_config().sync_client_batch_max_bytes
            budget = max(1, max_bytes - 128)
            # 评审 I1（W9 复审）：预算=**UTF-8 字节**（httpx 实发口径；CJK 3B/
            # 字符、emoji 4B——字符数在中文场景可低估约 3×，等于尺寸闸失效）。
            # 事件数同限 500 对齐服务端 PushRequest.events max_length（评审 M5：
            # batch>500 时会 422 重试空转）。
            max_events = min(get_config().sync_client_batch, 500)
            chunks: list[list[dict]] = []
            cur: list[dict] = []
            est = 0
            for item in items:
                n = len(json.dumps(item, ensure_ascii=False, default=str).encode("utf-8"))
                if cur and (est + n > budget or len(cur) >= max_events):
                    chunks.append(cur)
                    cur, est = [], 0
                cur.append(item)
                est += n
            if cur:
                chunks.append(cur)
            offset = 0
            for chunk in chunks:
                chunk_rows = rows[offset:offset + len(chunk)]
                offset += len(chunk)
                r = await self._call(state, client, "POST", "/api/sync/push",
                                     json={"device_id": state.device_id, "events": chunk})
                # 远端 skipped = 本端变更被仲裁拒收（LWW 败方/归属拒绝）——留痕，
                # 否则真实用户编辑会无声消失（冲突判据「败方留痕」覆盖 push 方向）
                skipped_ids = set(r.json().get("skipped", []))
                if skipped_ids:
                    by_id = {e.entity_id: e for e in chunk_rows}
                    for entity_id in skipped_ids:
                        ev = by_id.get(entity_id)
                        if ev is not None:
                            db.add(SyncConflict(
                                user_id=user.id, entity_type=ev.entity_type, entity_id=entity_id,
                                direction="push", reason="skipped_remote",
                                local_updated_at=parse_dt((ev.payload or {}).get("updated_at")),
                                remote_updated_at=None,
                            ))
                state.push_cursor = chunk_rows[-1].seq
                await db.commit()
            if len(rows) < batch:
                return

    # ── 入站（远端 delta → 本地应用）─────────────────────────────
    async def _pull(self, db, client, user, state) -> int:
        """返回本周期 apply 失败事件数（供 last_error 留痕）。"""
        batch = get_config().sync_client_batch
        total_errors = 0
        while True:
            r = await self._call(
                state, client, "GET",
                f"/api/sync/delta?cursor={state.pull_cursor or 0}&limit={batch}&device_id={state.device_id}",
            )
            body = r.json()
            apply_errors = 0
            for ev in body.get("events", []):
                # per-event SAVEPOINT（与 S1 服务端 push 同款隔离）——单条毒事件
                # （schema 漂移/约束违例）只回滚自身，绝不顶死整条 pull 流水线
                try:
                    async with db.begin_nested():
                        await self._apply_remote(db, user, state, ev)
                except Exception as exc:  # noqa: BLE001
                    apply_errors += 1
                    logger.warning(
                        "sync pull event apply failed (%s %s %s): %s",
                        ev.get("entity_type"), ev.get("entity_id"), ev.get("op"), exc,
                    )
                    db.add(SyncConflict(
                        user_id=user.id, entity_type=str(ev.get("entity_type", ""))[:40],
                        entity_id=str(ev.get("entity_id", ""))[:36],
                        direction="pull", reason="apply_error",
                        local_updated_at=None, remote_updated_at=None,
                    ))
            total_errors += apply_errors
            # 8a 客户端契约（W9，用户指令）：缺口信号 → 放弃增量、登记全量基线
            # （下一周期 _cycle_one 头部经 _resync_pending 消费）；本页不推进
            # 游标——防跨过缺口后信号永不可见。已应用事件由 resync 快照幂等重建。
            if body.get("resync_required"):
                self._resync_pending.add(user.id)
                logger.warning(
                    "sync delta gap detected (user=%s cursor=%s min_available=%s) "
                    "— full resync scheduled",
                    user.id[:8], state.pull_cursor or 0, body.get("min_available_seq"),
                )
                await db.commit()
                return total_errors
            state.pull_cursor = body.get("cursor", state.pull_cursor or 0)
            await db.commit()
            if not body.get("has_more"):
                if user.id in self._memory_touched:
                    # S5：记忆行拉取应用后本地重算嵌入（向量每设备本地重算策略；
                    # 端点不可用时保留 NULL 待下轮——repair 内部有界容错）
                    self._memory_touched.discard(user.id)
                    try:
                        from app.services.memory_concept_service import repair_missing_embeddings

                        fixed = await repair_missing_embeddings(db, user.id)
                        if fixed:
                            logger.info("sync memory re-embed: fixed=%d (user=%s)", fixed, user.id[:8])
                            await db.commit()
                    except Exception as exc:  # noqa: BLE001
                        # 失败必须回滚事务（中止态会连坐 last_sync_at 提交）
                        await db.rollback()
                        logger.warning("sync memory re-embed failed (下轮续): %s", exc)
                return total_errors

    async def _apply_remote(self, db, user, state, ev: dict) -> None:
        # 冲突预读：LWW 域 create/update 且本地已有行时比对 updated_at
        local_ts = None
        model = None
        if ev.get("op") in ("create", "update"):
            from app.services.sync_capture import MEMORY_SYNC_ENTITIES, SYNC_ENTITIES

            # 记忆域同样参与败方留痕
            model = {**SYNC_ENTITIES, **MEMORY_SYNC_ENTITIES}.get(ev.get("entity_type", ""))
            if model is not None and ev.get("entity_type") not in ("messages",):
                row = await db.get(model, ev["entity_id"])
                if row is not None:
                    local_ts = getattr(row, "updated_at", None)
        remote_ts = parse_dt((ev.get("payload") or {}).get("updated_at"))

        origin_ctx = set_origin_device(f"rx:{ev.get('origin_device') or 'cloud'}")
        try:
            result = await apply_change(
                db, user, ev["entity_type"], ev["entity_id"], ev["op"], ev.get("payload")
            )
        finally:
            reset_origin_device(origin_ctx)
        if result == APPLIED and ev.get("entity_type") in (
            "memory_episodes", "memory_concepts"
        ):
            self._memory_touched.add(user.id)

        if result == STALE and local_ts and remote_ts and local_ts > remote_ts:
            db.add(SyncConflict(
                user_id=user.id, entity_type=ev["entity_type"], entity_id=ev["entity_id"],
                direction="pull", reason="lww_local_newer",
                local_updated_at=local_ts, remote_updated_at=remote_ts,
            ))
        elif (
            result == APPLIED and local_ts and remote_ts and local_ts != remote_ts
            and state.last_sync_at and local_ts > state.last_sync_at
        ):
            # 远端覆盖了上次成功同步之后的本地改动（真实双端冲突的败方=本地）
            db.add(SyncConflict(
                user_id=user.id, entity_type=ev["entity_type"], entity_id=ev["entity_id"],
                direction="pull", reason="lww_remote_newer",
                local_updated_at=local_ts, remote_updated_at=remote_ts,
            ))

    # ── workspace 文件物化（2026-09-29 波）──────────────────────────────
    # 云端 workspace → 本地：全量同步自动拉取小文件（resync 钩子调用；超出
    # [sync] workspace_pull_cap 的部分跨周期续拉），大文件由预览端经
    # /api/sync/workspace/fetch 按需触发。复用远端既有 /api/files/list +
    # /api/files/download（生产 chatbot 后端同款，零服务端改动）。

    @staticmethod
    def _state_proxy(state):
        """绑定行取值快照（纯值对象）：供 ensure_user_workspace 的 commit 之后
        继续安全使用（ORM 行 commit 后过期，跨会话访问有 MissingGreenlet 前科）。"""
        from types import SimpleNamespace

        return SimpleNamespace(
            user_id=state.user_id,
            server_url=state.server_url,
            cloud_username=state.cloud_username,
            cloud_password=state.cloud_password,
            device_id=state.device_id,
            tls_verify=state.tls_verify,
            sync_enabled=state.sync_enabled,
        )

    _WORKSPACE_LIST_PAGE = 1000  # 远端单页上限（与服务端 MAX_DIRECTORY_ENTRIES 一致）

    _MAX_LISTING_BYTES = 32 * 1024 * 1024  # listing 响应体流式上限（评审 Minor R2）

    async def _remote_list(self, state, client, rel: str, offset: int = 0):
        """远端目录清单（分页契约，D-30）；404 → None（目录不存在）。

        旧服务端忽略 offset/limit 查询参数、响应无 offset/total 字段——
        walk_remote/stat 以此识别并停止翻页（兼容不回归）。响应体流式读取并
        限额（超限即断），不把超大/恶意 listing 无界缓冲进内存。
        """
        params = {"path": rel, "offset": offset, "limit": self._WORKSPACE_LIST_PAGE}
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=15.0)
        for attempt in (1, 2):
            if self._tkey(state) not in self._tokens:
                await self._login(state, client)
            headers = {"Authorization": f"Bearer {self._tokens[self._tkey(state)]}"}
            async with client.stream(
                "GET", "/api/files/list", params=params, headers=headers, timeout=timeout,
            ) as r:
                if r.status_code == 401 and attempt == 1:
                    await r.aclose()
                    await self._login(state, client)
                    continue
                if r.status_code == 404:
                    return None
                if r.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        f"listing HTTP {r.status_code}", request=r.request, response=r,
                    )
                body = bytearray()
                async for chunk in r.aiter_bytes(1 << 20):
                    body.extend(chunk)
                    if len(body) > self._MAX_LISTING_BYTES:
                        raise ValueError(f"remote listing too large (>32MB): {rel!r}")
            try:
                return json.loads(bytes(body))
            except ValueError as exc:
                raise ValueError(f"invalid listing JSON for {rel!r}") from exc
        return None

    async def sync_workspace_files(self, client, user_id: str, username, state) -> dict:
        """全量同步（resync）时把云端 workspace 中 <[sync] workspace_auto_max_mb
        （默认 10MB）且本地缺失/远端更新的文件物化到本地；单文件失败不连坐。"""
        cfg = get_config()
        if not cfg.sync_workspace_enabled:
            return {"skipped": "disabled"}
        from app.services.workspace_service import ensure_user_workspace
        from app.services.workspace_sync_service import (
            local_file_state,
            safe_target,
            should_pull,
            sweep_stale_syncparts,
            walk_remote,
        )

        async with AsyncSessionLocal() as db_ws:
            workspace = await ensure_user_workspace(db_ws, user_id, username)
            root = Path(workspace.root_path)

        # 崩溃残留 .syncpart 清扫（每用户每小时至多一次；有界本地遍历）。
        # 失败退避 10 分钟再试（评审 Minor：时间戳先盖会让瞬时失败停摆 1h）
        now = asyncio.get_running_loop().time()
        if now - self._workspace_swept.get(user_id, 0.0) > 3600:
            try:
                removed = await asyncio.to_thread(sweep_stale_syncparts, root)
                self._workspace_swept[user_id] = now
                if removed:
                    logger.info("workspace sweep: removed %d stale .syncpart (user=%s)",
                                removed, user_id[:8])
            except Exception as exc:  # noqa: BLE001 — 清扫失败不影响拉取
                self._workspace_swept[user_id] = now - 3000  # 10 分钟后重试
                logger.warning("workspace sweep failed (user=%s): %s", user_id[:8], exc)

        truncated: list = []

        async def fetch(rel, offset=0):
            return await self._remote_list(state, client, rel, offset)

        manifest = await walk_remote(fetch, on_truncated=truncated.append)
        if truncated:
            # 远端单目录 >1000（API 无分页）：被截断文件不会自动拉取，
            # 预览按需 fetch 仍可直达（stat 未知→直接尝试下载）
            logger.warning("workspace pull: %d remote listing(s) truncated (auto-pull skips overflow): %s",
                           len(truncated), truncated[:5])
        auto_max = cfg.sync_workspace_auto_max_mb * 1024 * 1024
        # 失败退避（评审 Important #2）：同一文件连续失败 ≤3 次后从候选中剔除，
        # 否则失败项永久占据预算头部 → 尾部文件永不尝试且 pending 永不清。
        candidates = [
            entry for entry in manifest
            if entry["size"] < auto_max
            and self._workspace_fail.get((user_id, entry["rel_path"]), 0) < 3
            and should_pull(entry, local_file_state(root, entry["rel_path"]))
        ]
        cap = cfg.sync_workspace_pull_cap
        budget = candidates[:cap]
        # 时间预算（评审 Important R2）：慢速/涓流远端不得无限占用周期
        budget_s = cfg.sync_workspace_pull_budget_seconds
        deadline = (asyncio.get_running_loop().time() + budget_s) if budget_s > 0 else None
        pulled = failed = 0
        processed = 0
        for entry in budget:
            if deadline is not None and asyncio.get_running_loop().time() > deadline:
                logger.warning(
                    "workspace pull budget exhausted (%ss); deferring %d remaining (user=%s)",
                    budget_s, len(budget) - processed, user_id[:8],
                )
                break
            processed += 1
            rel = entry["rel_path"]
            key = (user_id, rel)
            try:
                target = safe_target(root, rel)
            except ValueError as exc:
                failed += 1
                self._workspace_fail[key] = self._workspace_fail.get(key, 0) + 1
                logger.warning("workspace pull skipped invalid path %r: %s", rel, exc)
                continue
            res = await self._download_workspace_file(
                state, client, rel, target,
                max_bytes=auto_max, expected_mtime=entry.get("mtime"),
                deadline=deadline,
            )
            if res.get("ok"):
                pulled += 1
                self._workspace_fail.pop(key, None)
            else:
                failed += 1
                self._workspace_fail[key] = self._workspace_fail.get(key, 0) + 1
                logger.warning("workspace pull failed %s: %s", rel, res.get("reason"))
        # 超出本周期预算/时间预算（或被退避后的剩余候选）→ 登记 pending，后续
        # 周期续拉直至全部物化；永久失败项退避 3 次后不再重试（预览按需是兜底）。
        more = len(candidates) > processed
        if more:
            self._workspace_pending.add(user_id)
        else:
            self._workspace_pending.discard(user_id)
        logger.info(
            "sync workspace full-pull: remote=%d auto_max_mb=%d candidates=%d pulled=%d failed=%d "
            "more=%s truncated=%d",
            len(manifest), cfg.sync_workspace_auto_max_mb, len(candidates), pulled, failed,
            more, len(truncated),
        )
        return {
            "remote_files": len(manifest), "candidates": len(candidates),
            "pulled": pulled, "failed": failed, "more": more,
            "deferred": max(0, len(candidates) - processed),
            "truncated_dirs": len(truncated),
        }

    async def workspace_file_status(self, rel_path: str, user_id: str) -> dict:
        """预览前状态：本地是否已有；缺失且已绑定 → 远端是否存在/大小。"""
        from app.services.workspace_sync_service import (
            local_file_state,
            normalize_workspace_path,
            remote_workspace_stat,
        )

        cfg = get_config()
        out = {
            "bound": False,
            "local_exists": False, "local_size": None,
            "remote_exists": None, "remote_size": None,
            "auto_max_mb": cfg.sync_workspace_auto_max_mb,
            "max_mb": cfg.sync_workspace_max_mb,
        }
        rel_path = normalize_workspace_path(rel_path)
        if rel_path is None:
            return out
        proxy = None
        async with AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
            state = await db.get(SyncState, user_id)
            if user is None or state is None:
                return out
            proxy = self._state_proxy(state)
            from app.services.workspace_service import ensure_user_workspace

            workspace = await ensure_user_workspace(db, user.id, user.username)
            local = local_file_state(workspace.root_path, rel_path)
        # bound 先算（评审 Minor：本地已有时也应如实回显绑定态）
        out["bound"] = bool(proxy.sync_enabled and proxy.server_url)
        out["local_exists"] = bool(local["exists"])
        out["local_size"] = local["size"]
        if local["exists"] or not out["bound"]:
            return out
        try:
            async with self._client(
                proxy.server_url,
                proxy.tls_verify if proxy.tls_verify is not None else True,
            ) as client:
                async def fetch(rel, offset=0):
                    return await self._remote_list(proxy, client, rel, offset)

                remote = await remote_workspace_stat(fetch, rel_path)
            # exists 三态：True/False/None（父目录 listing 截断未命中→未知）
            out["remote_exists"] = remote["exists"]
            out["remote_size"] = remote["size"]
        except Exception as exc:  # noqa: BLE001 — 状态探测失败=未知（前端走原语义）
            logger.warning("workspace status remote probe failed (%s): %s", rel_path, exc)
        return out

    async def fetch_workspace_file(self, rel_path: str, user_id: str) -> dict:
        """按需单文件同步（预览「同步」按钮入口；上限 [sync] workspace_max_mb）。"""
        from app.services.workspace_sync_service import (
            local_file_state,
            normalize_workspace_path,
            remote_workspace_stat,
            safe_target,
            should_pull,
        )

        cfg = get_config()
        rel_path = normalize_workspace_path(rel_path)
        if rel_path is None:
            return {"ok": False, "reason": "invalid_path"}
        async with AsyncSessionLocal() as db:
            state = await db.get(SyncState, user_id)
            user = await db.get(User, user_id)
            if (state is None or user is None
                    or not state.sync_enabled or not state.server_url):
                return {"ok": False, "reason": "not_bound"}
            proxy = self._state_proxy(state)
            from app.services.workspace_service import ensure_user_workspace

            workspace = await ensure_user_workspace(db, user.id, user.username)
            root = Path(workspace.root_path)
        try:
            target = safe_target(root, rel_path)
        except ValueError as exc:
            return {"ok": False, "reason": "invalid_path", "detail": str(exc)}
        max_bytes = cfg.sync_workspace_max_mb * 1024 * 1024
        # 锁表有界（评审 Minor）：容量超限时清掉未持有的锁（活跃锁不动）
        locks = type(self)._FETCH_LOCKS
        if len(locks) > 4096:
            for stale_key in [k for k, lk in locks.items() if not lk.locked()][:2048]:
                locks.pop(stale_key, None)
        lock = locks.setdefault(("ws", proxy.device_id, rel_path), asyncio.Lock())
        async with lock:
            async with self._client(
                proxy.server_url,
                proxy.tls_verify if proxy.tls_verify is not None else True,
            ) as client:
                try:
                    async def fetch(rel, offset=0):
                        return await self._remote_list(proxy, client, rel, offset)

                    remote = await remote_workspace_stat(fetch, rel_path)
                except Exception as exc:  # noqa: BLE001 — 网络异常统一 502
                    logger.warning("workspace fetch stat failed %s: %s", rel_path, exc)
                    return {"ok": False, "reason": "network"}
                if remote["exists"] is False:
                    return {"ok": False, "reason": "remote_missing"}
                # exists=None（父目录截断未命中）→ 未知，直接尝试下载，404 兜底
                local = local_file_state(root, rel_path)
                if local["exists"] and not should_pull(remote, local):
                    # 用户点击期间本地已就位（并发自动拉取/编辑）→ 本地优先，不覆盖
                    return {"ok": True, "path": rel_path, "size": local["size"],
                            "note": "already_local"}
                if remote["size"] is not None and remote["size"] > max_bytes:
                    return {"ok": False, "reason": "too_large", "size": remote["size"]}
                res = await self._download_workspace_file(
                    proxy, client, rel_path, target,
                    max_bytes=max_bytes, expected_mtime=remote.get("mtime"),
                )
        if res.get("ok"):
            return {"ok": True, "path": rel_path, "size": res.get("size", 0)}
        return res

    async def _download_workspace_file(
        self, state, client, rel: str, target: Path,
        max_bytes: int, expected_mtime=None, allow_retry: bool = True,
        deadline: float | None = None,
    ) -> dict:
        """流式落盘：unique .part → os.replace 原子替换；超限即停并清理；
        mtime 对齐远端（manifest / Last-Modified），保证跨周期比较稳定。

        ``deadline``（loop 时刻，评审 Important R2）：整文件总时限——涓流
        远端不得让单个下载无限占用周期；超时清理 .part 并按 network 返回。
        """
        from email.utils import parsedate_to_datetime

        from app.services.workspace_sync_service import sanitize_remote_mtime

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "reason": "local_error", "detail": str(exc)}
        part = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.syncpart")
        timeout = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=15.0)
        total = 0
        oversized = False
        header_mtime = None
        total_timeout = None
        if deadline is not None:
            total_timeout = max(1.0, deadline - asyncio.get_running_loop().time())
        try:
            async with asyncio.timeout(total_timeout):
                if self._tkey(state) not in self._tokens:
                    await self._login(state, client)
                headers = {"Authorization": f"Bearer {self._tokens[self._tkey(state)]}"}
                async with client.stream(
                    "GET", "/api/files/download",
                    headers=headers, params={"path": rel}, timeout=timeout,
                ) as r:
                    if r.status_code == 401 and allow_retry:
                        await r.aclose()
                        await self._login(state, client)
                        return await self._download_workspace_file(
                            state, client, rel, target, max_bytes,
                            expected_mtime=expected_mtime, allow_retry=False,
                            deadline=deadline,
                        )
                    if r.status_code == 404:
                        return {"ok": False, "reason": "remote_missing"}
                    if r.status_code != 200:
                        # 非 200（含 3xx 未跟随/其它 4xx/5xx）不得把响应体当文件写盘
                        # （评审 Minor：代理重定向页会被永久落盘且 mtime 对齐）
                        return {"ok": False, "reason": "remote_error", "status": r.status_code}
                    header_mtime = r.headers.get("last-modified")
                    with part.open("wb") as f:
                        async for chunk in r.aiter_bytes(1 << 20):
                            total += len(chunk)
                            if total > max_bytes:
                                oversized = True
                                break  # 不边写边删（Windows 打不开删除句柄）
                            f.write(chunk)
        except TimeoutError:
            part.unlink(missing_ok=True)
            return {"ok": False, "reason": "timeout"}
        except httpx.HTTPError as exc:
            part.unlink(missing_ok=True)
            return {"ok": False, "reason": "network", "detail": str(exc)}
        except OSError as exc:
            part.unlink(missing_ok=True)
            return {"ok": False, "reason": "local_error", "detail": str(exc)}
        if oversized:
            part.unlink(missing_ok=True)
            return {"ok": False, "reason": "too_large"}
        try:
            os.replace(part, target)
        except OSError as exc:
            part.unlink(missing_ok=True)
            return {"ok": False, "reason": "local_error", "detail": str(exc)}
        # mtime 消毒（评审 Important #3）：远端来源值先过 finite/未来窗口校验，
        # 落盘异常（OverflowError/ValueError/OSError）一律忽略——文件已就位，
        # 不能因时间戳损坏把成功下载报成失败（resync 预算中途弃跑）。
        mtime = sanitize_remote_mtime(expected_mtime)
        if mtime is None and header_mtime:
            try:
                mtime = sanitize_remote_mtime(parsedate_to_datetime(header_mtime).timestamp())
            except (TypeError, ValueError, OverflowError):
                mtime = None
        if mtime is not None:
            try:
                os.utime(target, (mtime, mtime))
            except (OSError, OverflowError, ValueError):
                pass
        return {"ok": True, "size": total}

    # ── blob 上行（本地 sync_blobs diff 远端列表 → eager 上传）─────────────
    _BLOB_UPLOAD_CAP = 5  # 单周期单账号上传上限（防大文件挤占事件同步）

    async def _blobs_push(self, db, client, user, state) -> None:
        from app.db.database import SyncBlob

        remote_shas: set = set()
        since: str | None = None
        after: str | None = None
        while True:
            q = "/api/sync/blobs?limit=1000"
            if since:
                q += f"&since={since}"
            if after:
                q += f"&after={after}"
            body = (await self._call(state, client, "GET", q)).json()
            items = body.get("items", [])
            remote_shas.update(i["sha256"] for i in items)
            since = body.get("next_since")
            after = body.get("next_after")
            if not since or not items:
                break
        local_rows = (
            await db.execute(
                select(SyncBlob).where(SyncBlob.user_id == user.id).order_by(SyncBlob.created_at)
            )
        ).scalars().all()
        # 退避：进程内失败计数 ≥3 的 blob 跳过（防永久失败行吃满 cap 饿死健康上传）
        missing = [
            r for r in local_rows
            if r.sha256 not in remote_shas and self._blob_fail.get((state.device_id, r.sha256), 0) < 3
        ]
        blob_dir = get_config().sync_blob_dir
        for row in missing[: self._BLOB_UPLOAD_CAP]:
            # per-PUT 隔离（单坏 blob 不得顶死上传流水线）；路径由 sha 重算而非信
            # DB row.path（防行被改写出任意文件外发面）
            key = (state.device_id, row.sha256)
            path = blob_dir / row.sha256
            if not path.exists():
                self._blob_fail[key] = self._blob_fail.get(key, 0) + 1
                logger.warning("sync blob file missing locally: %s", path)
                continue
            try:
                await self._call(
                    state, client, "PUT", f"/api/sync/blobs/{row.sha256}",
                    content=path.read_bytes(),
                    headers={"Content-Type": "application/octet-stream"},
                )
                logger.info("sync blob uploaded: %s (%dB, user=%s)", row.sha256[:12], row.size, user.id[:8])
                self._blob_fail.pop(key, None)
            except Exception as exc:  # noqa: BLE001
                self._blob_fail[key] = self._blob_fail.get(key, 0) + 1
                logger.warning("sync blob upload failed %s: %s", row.sha256[:12], exc)

    # ── blob 懒下载穿透（端点调用；按调用用户的绑定回源）────────────────
    _FETCH_LOCKS: dict = {}

    async def fetch_blob(self, sha256: str, user_id: str) -> bool:
        """端点穿透入口：per-sha 锁（慢 fetch 不串行全局；防同 sha 竞态共写 .part）。
        负缓存 60s：远端 404 的 sha 短期内不再回源。"""
        async with AsyncSessionLocal() as db:
            state = await db.get(SyncState, user_id)
        if state is None or not state.sync_enabled or not state.server_url:
            return False
        now = asyncio.get_running_loop().time()
        neg = self._neg_cache.get((state.device_id, sha256))
        if neg and neg > now:
            return False
        lock = type(self)._FETCH_LOCKS.setdefault((state.device_id, sha256), asyncio.Lock())
        async with lock:
            ok = await self._fetch_blob_inner(sha256, state)
        if not ok:
            self._neg_cache[(state.device_id, sha256)] = now + 60
        return ok

    async def _fetch_blob_inner(self, sha256: str, state) -> bool:
        """从该用户绑定的远端拉取 blob 到本地。返回是否成功落盘成行。

        流式下载（aiter_bytes 逐块写 .part 并增量哈希——进度真实可续，超时不再
        迫使整文件重来）；.part 续传：Range: bytes=<size>-（206 追加；200 全量
        重写）；sha 实算不符且本轮为续传 → 删 .part 全量重试一次（坏前缀恢复），
        再不符 → False（.part 清除，不卡死 blob）。
        远端 404 → False（本地 404 语义）；远端其余异常 → HTTPException(502)。
        """
        cfg = get_config()
        if not cfg.sync_enabled:
            return False
        from app.db.database import SyncBlob
        from fastapi import HTTPException

        blob_dir = cfg.sync_blob_dir
        blob_dir.mkdir(parents=True, exist_ok=True)
        final = blob_dir / sha256
        part = blob_dir / f".{sha256}.part"
        timeout = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=15.0)
        try:
            for attempt in (1, 2):
                resume_from = part.stat().st_size if (attempt == 1 and part.exists()) else 0
                async with self._client(state.server_url, state.tls_verify if state.tls_verify is not None else True) as client:
                    if not self._tokens.get(self._tkey(state)):
                        await self._login(state, client)
                    headers = {"Authorization": f"Bearer {self._tokens[self._tkey(state)]}"}
                    if resume_from:
                        headers["Range"] = f"bytes={resume_from}-"
                    async with client.stream(
                        "GET", f"/api/sync/blobs/{sha256}", headers=headers, timeout=timeout
                    ) as r:
                        if r.status_code == 404:
                            part.unlink(missing_ok=True)  # 远端无 → 清断点
                            return False
                        if r.status_code == 401:
                            await self._login(state, client)
                            headers["Authorization"] = f"Bearer {self._tokens[self._tkey(state)]}"
                            async with client.stream(
                                "GET", f"/api/sync/blobs/{sha256}", headers=headers, timeout=timeout
                            ) as r2:
                                if r2.status_code == 404:
                                    part.unlink(missing_ok=True)
                                    return False
                                if r2.status_code >= 400:
                                    raise HTTPException(status_code=502, detail="sync remote blob error")
                                resumed = resume_from and r2.status_code == 206
                                digest = await self._stream_to_part(part, resumed, r2)
                        else:
                            if r.status_code >= 400:
                                raise HTTPException(status_code=502, detail="sync remote blob error")
                            resumed = resume_from and r.status_code == 206
                            digest = await self._stream_to_part(part, resumed, r)
                    if digest == sha256:
                        part.replace(final)
                        async with AsyncSessionLocal() as db:
                            row = await db.get(SyncBlob, (sha256, state.user_id))
                            if row is None:
                                db.add(SyncBlob(
                                    sha256=sha256, user_id=state.user_id,
                                    size=final.stat().st_size, path=str(final),
                                ))
                                try:
                                    await db.commit()
                                except IntegrityError:
                                    await db.rollback()  # 并发同行（双锁外路径）→ 幂等
                        return True
                    # sha 不符：续传轮 → 删 .part 全量重试一次；全量轮 → 放弃
                    if attempt == 1 and resume_from:
                        logger.warning(
                            "sync blob sha mismatch after resume (%s) — 删 .part 全量重试",
                            sha256[:12],
                        )
                        part.unlink(missing_ok=True)
                        continue
                    logger.warning("sync blob sha mismatch on full fetch (%s)", sha256[:12])
                    part.unlink(missing_ok=True)
                    return False
            return False
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="sync remote unreachable") from exc

    @staticmethod
    def _hash_prefix(part: Path, h) -> None:
        with part.open("rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)

    async def _stream_to_part(self, part: Path, resumed, response) -> str:
        """把响应体流式写进 .part（resumed=追加）并返回全量 sha256（含已有前缀；
        前缀重哈希在线程中跑，不阻塞事件循环）。"""
        h = hashlib.sha256()
        if resumed:
            await asyncio.to_thread(self._hash_prefix, part, h)
        mode = "ab" if resumed else "wb"
        with part.open(mode) as f:
            async for chunk in response.aiter_bytes(1 << 20):
                h.update(chunk)
                f.write(chunk)
        return h.hexdigest()


sync_client = SyncClient()
