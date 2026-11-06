"""真实 PostgreSQL 回归（2026-10-22）：agent_worker 结果保存块同形状
（existing_conv + 新 Message(290KB tool_results) + Query-invoked autoflush
+ note flush + commit）不得触发 message_payloads_message_id_fkey 违规。

环境契约（A4.9 re-review 轮 3 Critical-1 / Important-1 收口）：
- **DSN 解析**：`WT_TEST_PG_URL`（CI：ci.yml 后端 job 注入 service 凭据）优先；
  未设置时用应用配置的 `config.database_url`（本地 dev）。CI 服务与 tracked
  config.toml 的凭据不同——靠 env 桥接，不硬编码任何凭据。
- **自有 engine**：本测试自建临时 async engine + sessionmaker（不碰应用全局
  engine），create_all 所需 8 表（checkfirst 幂等），结束后 dispose——零跨测试
  污染、零全局池残留。
- **skip 规则**：env DSN 存在时**永不 skip**（CI 健康服务下任何失败都是真故障）；
  未设置时仅连接级失败或非本机 host（60 箱共享生产库）才 skip。
- 确定性 hermetic 网仍是 `test_message_payload_newmsg_fk.py`（sqlite 行为烟 +
  mapper 结构契约）；独立排障脚本为 tests/repro_worker_fk_20261022.py。
"""
import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime

import pytest
from sqlalchemy.exc import InterfaceError, OperationalError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_cleanup_logger = logging.getLogger(__name__)

# FK 依赖闭包：users → conversation_groups/assistants → conversations →
# messages → message_payloads；notebooks → notes（测试体写 note flush）。
_TABLES = ("users", "conversation_groups", "assistants",
           "conversations", "messages", "message_payloads",
           "notebooks", "notes")


def _nonlocal_host_guard(host: str, env_dsn_set: bool) -> bool:
    """True = 本机/CI 形态（允许运行）；False = 共享库（默认 skip）。

    60 箱 `[database]` 指向 <db-host> 共享多租户生产库——默认不跑；
    显式 `WT_TEST_PG_URL` 或 `WT_PG_REGRESSION=1` 才放行。
    """
    if env_dsn_set or os.environ.get("WT_PG_REGRESSION") == "1":
        return True
    return (host or "").strip().lower() in ("127.0.0.1", "localhost", "::1")


def _skip_reason_connection(exc: BaseException) -> bool:
    msg = f"{type(exc).__name__}: {exc}".lower()
    return (
        isinstance(exc, (OperationalError, InterfaceError, OSError, ConnectionError))
        or "cannot connect" in msg
        or "connection refused" in msg
        or "could not connect" in msg
        or "connect call failed" in msg
        or "name or service not known" in msg
        or "nodename nor servname" in msg
    )


def test_nonlocal_host_guard_contract(monkeypatch):
    """Important-1 回归：守卫必须真的会拦截共享库（旧实现读不存在的
    config.database → 守卫恒真、死代码）。re-review 轮 4 Minor-1：
    `WT_PG_REGRESSION=1` 是文档化逃生口，不得把本单测染红。"""
    monkeypatch.delenv("WT_PG_REGRESSION", raising=False)
    assert _nonlocal_host_guard("127.0.0.1", False) is True
    assert _nonlocal_host_guard("localhost", False) is True
    assert _nonlocal_host_guard("<db-host>", False) is False
    assert _nonlocal_host_guard("::1", False) is True
    assert _nonlocal_host_guard("<db-host>", True) is True
    monkeypatch.setenv("WT_PG_REGRESSION", "1")
    assert _nonlocal_host_guard("<db-host>", False) is True


def test_worker_save_shape_no_fk_violation_on_real_pg():
    async def _run():
        from sqlalchemy import delete, select, text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from app.core.config import get_config
        from app.db.database import (
            Base, Conversation, Message, MessagePayload,
            Note, Notebook, User,
        )
        from app.services.message_payload_service import install_message_payload_hook

        env_dsn = (os.environ.get("WT_TEST_PG_URL") or "").strip()
        cfg = get_config()
        dsn = env_dsn or cfg.database_url
        engine = create_async_engine(dsn)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            if not env_dsn:
                if not _nonlocal_host_guard(cfg.database_host, env_dsn_set=False):
                    pytest.skip(
                        f"非本机 PostgreSQL({cfg.database_host})——共享库默认跳过；"
                        "显式设 WT_TEST_PG_URL 或 WT_PG_REGRESSION=1 才运行")
                try:
                    async with maker() as db:
                        await db.execute(text("SELECT 1"))
                except Exception as exc:  # noqa: BLE001
                    if _skip_reason_connection(exc):
                        pytest.skip(f"PostgreSQL 不可达（连接级）：{type(exc).__name__}")
                    raise

            # 自建 schema（CI 空库；本地既有库为 no-op）
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(
                        sync_conn,
                        tables=[Base.metadata.tables[n] for n in _TABLES],
                        checkfirst=True,
                    )
                )

            install_message_payload_hook()
            suffix = uuid.uuid4().hex[:8]
            username = f"pgfk_{suffix}"
            big = json.dumps({"rounds": [], "results": [], "search_failed": False,
                              "agent_steps": [{"content": "y" * (290 * 1024)}]},
                             ensure_ascii=False)
            try:
                async with maker() as db:
                    u = User(id=str(uuid.uuid4()), username=username,
                             password_hash="x", created_at=datetime.utcnow())
                    c = Conversation(id=str(uuid.uuid4()), user_id=u.id,
                                     assistant_id=None, title="pg fk")
                    nb = Notebook(id=str(uuid.uuid4()), user_id=u.id,
                                  name=f"nb_{suffix}", is_default=True)
                    db.add_all([u, c, nb])
                    await db.commit()
                    cid, uid, nbid = c.id, u.id, nb.id

                async with maker() as db:
                    conv = await db.get(Conversation, cid)
                    conv.updated_at = datetime.utcnow()
                    msg = Message(id=str(uuid.uuid4()), conversation_id=cid,
                                  role="assistant", content="z" * 5000,
                                  tool_results=big)
                    db.add(msg)
                    nb = (await db.execute(select(Notebook).where(
                        Notebook.user_id == uid))).scalar_one_or_none()
                    assert nb is not None
                    db.add(Note(id=str(uuid.uuid4()), notebook_id=nbid,
                                title="t", content="c"))
                    await db.flush()
                    await db.commit()

                    row = (await db.execute(select(MessagePayload).where(
                        MessagePayload.message_id == msg.id,
                        MessagePayload.field == "tool_results",
                    ))).scalars().first()
                    assert row is not None, "超限字段必须外置"
                    assert row.size_bytes == len(big.encode("utf-8"))
                    persisted = await db.get(Message, msg.id)
                    assert persisted is not None and persisted.tool_results != big
            finally:
                # 清理失败不得掩盖原始断言（Critical-1②：逐步独立兜底）。
                try:
                    async with maker() as db:
                        uid_sub = select(User.id).where(User.username == username)
                        cid_sub = select(Conversation.id).where(Conversation.user_id.in_(uid_sub))
                        nid_sub = select(Notebook.id).where(Notebook.user_id.in_(uid_sub))
                        await db.execute(delete(MessagePayload).where(
                            MessagePayload.message_id.in_(
                                select(Message.id).where(Message.conversation_id.in_(cid_sub)))))
                        await db.execute(delete(Note).where(Note.notebook_id.in_(nid_sub)))
                        await db.execute(delete(Notebook).where(Notebook.user_id.in_(uid_sub)))
                        await db.execute(delete(Message).where(Message.conversation_id.in_(cid_sub)))
                        await db.execute(delete(Conversation).where(Conversation.user_id.in_(uid_sub)))
                        await db.execute(delete(User).where(User.username == username))
                        await db.commit()
                except Exception:  # noqa: BLE001 — cleanup 不可掩盖根因，但必须可见
                    _cleanup_logger.warning(
                        "pg fk regression cleanup failed for %s (rows may remain)",
                        username, exc_info=True)
        finally:
            try:
                await engine.dispose()
            except Exception:
                pass

    asyncio.run(_run())
