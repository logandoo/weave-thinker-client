"""RED tests (A4.8) — 渐进落库 + 启动标记 + 被拒草稿台账（2026-10-06，conv ae9aa092）。

生产事故：部署重启强杀在途 run → 已流式展示的回答零落库消失；下一轮把未答问题
一并回答（重复感）。用户裁定：渐进落库 + 被拒草稿保留并标记。

契约：
  R1 Message.delivery_status ∈ streaming/final/interrupted（默认 final）；
     message_attempts 表承载被拒/中断草稿全文+裁决元数据。
  R2 run 开始即建行（streaming）；周期刷写（Δ≥2000 字符或 ≥5s 且增长）；
     单 flight（在途写未完成不并发写）。
  R3 finalize/stop/resume/interrupted-save 全部 update-in-place（有 streaming 行
     则更新不插入），终态 delivery_status 正确。
  R4 启动 sweep：streaming → interrupted。
  R5 默认消息列表/同步发射不含 streaming 行。
  R6 审计打回 → on_draft_rejected 回调携带全文+裁决元数据（fail-open 不炸 loop）。

Run: cd backend && python -m pytest tests/test_stream_persistence.py -v
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run(coro):
    return asyncio.run(coro)


# ── R1：模型与迁移 ────────────────────────────────────────────────────────

def test_message_model_delivery_status():
    from app.db.database import Message
    col = Message.__table__.columns.get("delivery_status")
    assert col is not None, "messages 缺 delivery_status 列"
    assert col.default is not None and col.default.arg == "final"


def test_message_attempt_model_shape():
    from app.db.database import MessageAttempt
    cols = set(MessageAttempt.__table__.columns.keys())
    assert {"id", "message_id", "conversation_id", "attempt_no",
            "status", "content", "verdict_json", "created_at"} <= cols


def test_startup_migrations_present():
    from app.db.migrations import STARTUP_MIGRATIONS
    versions = [v for v, _ in STARTUP_MIGRATIONS]
    assert "messages_delivery_status" in versions
    assert "message_attempts_v1" in versions


# ── R2：渐进刷写器（纯逻辑接缝）───────────────────────────────────────────

def test_progressive_flush_thresholds():
    from app.api.chat import ProgressiveFlusher
    writes = []
    now = [1000.0]

    async def _writer(text):
        writes.append(text)

    async def main():
        pf = ProgressiveFlusher(_writer, clock=lambda: now[0], min_chars=2000, min_seconds=5.0)
        pf.feed("a" * 500)   # 全量快照 500
        await asyncio.sleep(0)
        assert writes == [], "小增量不刷"
        pf.feed("a" * 2000)  # 全量快照 2000（累计增长 ≥2000）
        await asyncio.sleep(0)
        assert writes == ["a" * 2000], "Δ≥2000 字符触发刷写"
        pf.feed("a" * 2000 + "b" * 100)
        now[0] += 6.0
        pf.feed("a" * 2000 + "b" * 200)  # 距上次 >5s 且有增长
        await asyncio.sleep(0)
        assert writes[-1] == "a" * 2000 + "b" * 200, "≥5s 且有增长触发刷写"
        now[0] += 1.0
        pf.feed("a" * 2000 + "b" * 200 + "c" * 10)
        await asyncio.sleep(0)
        assert len(writes) == 2, "既无增量也无时隔不刷"

    _run(main())


def test_progressive_flush_single_flight():
    from app.api.chat import ProgressiveFlusher
    import asyncio as _aio
    started = []
    release = _aio.Event()

    async def _writer(text):
        started.append(text)
        await release.wait()

    pf = ProgressiveFlusher(_writer, min_chars=10, min_seconds=0.0)

    async def main():
        pf.feed("x" * 20)   # 触发第 1 次写（挂起）
        await _aio.sleep(0)
        pf.feed("x" * 20 + "y" * 20)  # 全量快照增长达标，但在途写未完成 → 不并发
        await _aio.sleep(0)
        assert len(started) == 1, "在途写未完成时不得并发第二次写"
        release.set()

    _run(main())


def test_progressive_revert_waits_and_retracts():
    """A4.9 r1 M1：打回撤回——在途写落地保序后把行内容撤回基线。"""
    from app.api.chat import ProgressiveFlusher
    import asyncio as _aio
    writes = []
    release = _aio.Event()

    async def _writer(text):
        writes.append(text)
        await release.wait()

    async def main():
        pf = ProgressiveFlusher(_writer, min_chars=10, min_seconds=0.0)
        pf.feed("被拒草稿全文" * 10)  # 触发在途写（挂起）
        await _aio.sleep(0)
        _aio.get_running_loop().call_later(0.05, release.set)  # 异步释放在途写
        await pf.revert_to("")  # 等在途写完成后撤回为空
        assert writes == ["被拒草稿全文" * 10, ""], f"撤回必须保序在在途写之后：{writes}"
        assert pf._last_len == 0
        pf.feed("新草稿" * 10)  # 撤回后增量刷写恢复
        await _aio.sleep(0.2)
        assert writes[-1] == "新草稿" * 10

    _run(main())


# ── R3：终态落库 update-in-place ─────────────────────────────────────────

def _fake_db_with_row(row):
    db = AsyncMock()
    scalars = AsyncMock()
    scalars.first = lambda: row
    exec_result = AsyncMock()
    exec_result.scalars = lambda: scalars
    db.execute = AsyncMock(return_value=exec_result)
    return db


def test_terminal_save_updates_streaming_row():
    from app.api.chat import _save_assistant_terminal
    from app.db.database import Message
    row = Message(conversation_id="c1", role="assistant", content="部分正文",
                  delivery_status="streaming")
    db = _fake_db_with_row(row)
    out = _run(_save_assistant_terminal(
        db, conversation_id="c1", content="完整正文", reasoning="r",
        tool_results='{"results":[]}', tool_calls=None, context_info=None,
        status="final"))
    assert out is row, "有 streaming 行必须更新原行"
    assert row.content == "完整正文" and row.delivery_status == "final"
    assert not db.add.called, "不得插入新行（重复回答）"


def test_terminal_save_inserts_when_no_streaming_row():
    from app.api.chat import _save_assistant_terminal
    db = _fake_db_with_row(None)
    db.add = lambda m: None
    row = _run(_save_assistant_terminal(
        db, conversation_id="c1", content="正文", reasoning=None,
        tool_results=None, tool_calls=None, context_info=None,
        status="final"))
    assert row.delivery_status == "final" and row.content == "正文"


def test_terminal_save_interrupted_status():
    from app.api.chat import _save_assistant_terminal
    from app.db.database import Message
    row = Message(conversation_id="c1", role="assistant", content="部分",
                  delivery_status="streaming")
    db = _fake_db_with_row(row)
    _run(_save_assistant_terminal(
        db, conversation_id="c1", content="部分", reasoning=None,
        tool_results=None, tool_calls=None, context_info=None,
        status="interrupted"))
    assert row.delivery_status == "interrupted"


# ── R4：启动 sweep（W4 项5 重写 2026-10-06，D-7）───────────────────────────
# 旧断言钉「sweep 走 Core bulk DELETE+UPDATE」——bulk DML 不触发 mapper 事件，
# sync_capture 捕获不到 streaming→interrupted 终态翻转（跨端不可见直至全量
# resync）；用户清单 UPSTREAM_TODO_20261006.md W4 项5 要求改走 ORM 实例更新。
# 断言按新契约重写（spec-test 冲突按 §V2 用户 spec 优先，见 tests/decisions.md D-7）。

from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _w4_jsonb_as_json(element, compiler, **kw):
    """测试方言桥：SyncEvent.payload 是 PG JSONB，sqlite 以 JSON 渲染
    （仿 test_reorder_emit_side_ts_bump.py 先例）。"""
    return "JSON"


@compiles(BigInteger, "sqlite")
def _w4_bigint_as_int(element, compiler, **kw):
    return "INTEGER"


class _AsyncShim:
    """sync Session → mark_interrupted_streams 期待的 async 接口（测试专用）。
    与 AsyncSession 同形：execute/delete/flush 均可 await（delete/flush 为协程）。"""

    def __init__(self, session):
        self._s = session

    async def execute(self, stmt):
        return self._s.execute(stmt)

    async def delete(self, obj):
        return self._s.delete(obj)

    async def flush(self):
        return self._s.flush()


class _sync_on:
    """置 [sync].enabled=True 供 _emit 发射（用后还原）。"""

    def __enter__(self):
        import app.core.config as _cfg
        self._cfg = _cfg.get_config()
        self._old = self._cfg._config.get("sync", {}).get("enabled")
        self._cfg._config.setdefault("sync", {})["enabled"] = True
        return self

    def __exit__(self, *exc):
        if self._old is None:
            self._cfg._config["sync"].pop("enabled", None)
        else:
            self._cfg._config["sync"]["enabled"] = self._old
        return False


def _sweep_env():
    """SQLite in-memory + 真 ORM 模型 + 方言桥。返回 (engine, conversation_id)。"""
    from app.db.database import Base, Conversation, Message, SyncEvent, SyncTombstone, User

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        User.__table__, Conversation.__table__,
        Message.__table__, SyncEvent.__table__, SyncTombstone.__table__,
    ])
    with Session(engine) as s:
        u = User(username="w4", password_hash="x")
        s.add(u)
        s.flush()
        c = Conversation(user_id=u.id, title="t")
        s.add(c)
        s.commit()
        return engine, c.id


def test_boot_sweep_marks_interrupted():
    """R4（W4 项5）：非空 streaming 行 ORM 属性翻 interrupted、空壳 await db.delete、
    返回翻转计数；两腿都经 ORM 漏斗（spy _emit 见 after_update/after_delete）。"""
    import app.services.sync_capture as sc
    from app.api.chat import mark_interrupted_streams
    from app.db.database import Message

    engine, conv_id = _sweep_env()
    sc.unregister_sync_capture()
    sc.register_sync_capture()
    with Session(engine) as s, _sync_on():
        m1 = Message(conversation_id=conv_id, role="assistant", content="部分正文",
                     delivery_status="streaming")
        m2 = Message(conversation_id=conv_id, role="assistant", content="",
                     delivery_status="streaming")
        s.add_all([m1, m2])
        s.commit()
        m1_id, m2_id = m1.id, m2.id
        calls = []
        real = sc._emit

        def spy(conn, et, op, target):
            calls.append((et, op, getattr(target, "delivery_status", None)))
            return real(conn, et, op, target)

        sc._emit = spy
        try:
            n = _run(mark_interrupted_streams(_AsyncShim(s)))
            s.commit()
        finally:
            sc._emit = real
            sc.unregister_sync_capture()
        assert n == 1
        assert s.get(Message, m1_id).delivery_status == "interrupted"
        assert s.get(Message, m2_id) is None
        assert ("messages", "update", "interrupted") in calls, \
            "翻转必须经 ORM after_update 漏斗（bulk DML 无 mapper 事件）"
        assert ("messages", "delete", "streaming") in calls, \
            "空壳删除必须经 ORM after_delete 漏斗"


def test_boot_sweep_flip_emits_sync_create():
    """W4 项5 事件链实证：翻转经 mapper after_update → sync_events 恰 1 条
    op=create（streaming 期 create 被跳过，终态翻转 update→create coerce）；
    空壳删除零 SyncEvent 零墓碑（该行 create 从未发射，delete 被 streaming 守卫抑制）。"""
    import app.services.sync_capture as sc
    from app.api.chat import mark_interrupted_streams
    from app.db.database import Message, SyncEvent, SyncTombstone

    engine, conv_id = _sweep_env()
    sc.unregister_sync_capture()
    sc.register_sync_capture()
    with Session(engine) as s, _sync_on():
        m1 = Message(conversation_id=conv_id, role="assistant", content="残稿正文",
                     delivery_status="streaming")
        m2 = Message(conversation_id=conv_id, role="assistant", content=None,
                     delivery_status="streaming")
        s.add_all([m1, m2])
        s.commit()
        m1_id = m1.id
        assert s.execute(select(SyncEvent)).scalars().all() == [], \
            "streaming 期 create 不发射（前置净空）"
        n = _run(mark_interrupted_streams(_AsyncShim(s)))
        s.commit()
        sc.unregister_sync_capture()
        events = s.execute(select(SyncEvent)).scalars().all()
        assert n == 1
        assert len(events) == 1, f"翻转必须发射且仅发射 1 条，实得 {len(events)}"
        ev = events[0]
        assert ev.entity_type == "messages" and ev.op == "create" and ev.entity_id == m1_id
        assert ev.payload["delivery_status"] == "interrupted"
        assert s.execute(select(SyncTombstone)).scalars().all() == [], \
            "空壳 delete 不得产生墓碑（对端从未有过该行）"


def test_boot_sweep_orm_path_source_pin():
    """W4 项5 防回退钉：sweep 不得使用 Core bulk DML（update()/delete() 语句不触发
    mapper 事件——SQLAlchemy flush-only，见 sync_capture 模块 docstring）。"""
    import inspect
    from app.api.chat import mark_interrupted_streams
    src = inspect.getsource(mark_interrupted_streams)
    assert "sa_update" not in src and "sa_delete" not in src, \
        "mark_interrupted_streams 禁止 bulk DML——必须 ORM 实例更新/删除"


def test_boot_sweep_after_sync_capture_registration():
    """W4 项5 接线钉（A4.9 双评审 Critical 回归）：main.py 启动序列必须先
    register_sync_capture() 再 boot sweep——mapper 事件在 sweep 提交的 flush 里
    触发，晚注册=事件时零监听器=零 SyncEvent（残稿依旧跨端不可见）。"""
    import os
    main_src = open(
        os.path.join(os.path.dirname(__file__), "..", "main.py"),
        encoding="utf-8").read()
    reg = main_src.index("register_sync_capture()")
    sweep = main_src.index("mark_interrupted_streams(_sweep_db)")
    assert reg < sweep, \
        "boot sweep 必须排在 sync_capture 注册之后（否则翻转零发射）"


# ── R5：列表/同步过滤 streaming ───────────────────────────────────────────

def test_message_list_excludes_streaming_rows():
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "app", "api", "conversation.py"),
        encoding="utf-8").read()
    # ORM 全量路径 + 原生 SQL 分页路径都必须排除 streaming
    assert src.count("delivery_status") >= 2, "两个消息列表路径都要过滤 streaming"
    assert "delivery_status != 'streaming'" in src or 'delivery_status != "streaming"' in src \
        or "delivery_status !=" in src


def test_sync_capture_skips_streaming_messages():
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "app", "services", "sync_capture.py"),
        encoding="utf-8").read()
    assert "delivery_status" in src, "sync 发射面必须排除 streaming 行"


def test_sync_capture_coerces_terminal_update_to_create():
    """A4.9 r1 B#1 + W4 项6b：streaming 期 create 被跳过——终态翻转的 update 若
    原样发射，对端无行可命中（update-without-create = 跨设备丢失）。terminal 态
    messages 的 update 必须 coerce 为 create（全行快照）发射——sync_apply 对
    messages：create 幂等插入、update 恒 SKIPPED、delete 存在即删；op 词表是
    create/update/delete，命名/文档/断言一致钉 create。"""
    import app.services.sync_capture as sc
    from app.db.database import Message

    captured = []

    class _FakeConn:
        def execute(self, stmt, params=None):
            captured.append((stmt, params))
            return AsyncMock()

    row = Message(conversation_id="c1", role="assistant", content="x",
                  delivery_status="final")
    row.id = "m1"

    import app.core.config as _cfg
    cfg = _cfg.get_config()
    old_sync = cfg._config.get("sync", {}).get("enabled")
    cfg._config.setdefault("sync", {})["enabled"] = True
    old_resolve = sc._resolve_user_id
    sc._resolve_user_id = lambda conn, et, target: "u1"
    try:
        sc._emit(_FakeConn(), "messages", "update", row)
        assert captured, "终态翻转必须发射"
        # SyncEvent.__table__.insert().values(...) —— compiled params 可见 op 值：
        # update 必须以 'create' 落 insert（coerce 钉，与命名一致）
        stmt = captured[0][0]
        params = dict(stmt.compile().params)
        assert params.get("op") == "create", "update 必须 coerce 为 create 发射"
        assert params.get("op") != "update", "不得原样发射 update（对端无行可命中）"
        captured.clear()
        row2 = Message(conversation_id="c1", role="assistant", content="",
                       delivery_status="streaming")
        row2.id = "m2"
        sc._emit(_FakeConn(), "messages", "create", row2)
        assert not captured, "streaming 行绝不发射"
    finally:
        sc._resolve_user_id = old_resolve
        if old_sync is None:
            cfg._config["sync"].pop("enabled", None)
        else:
            cfg._config["sync"]["enabled"] = old_sync


def test_sync_update_coercion_source_pin():
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "app", "services", "sync_capture.py"),
        encoding="utf-8").read()
    # scoped 复审 Critical：op 词表是 create/update/delete（sync_apply 对
    # messages：create 幂等插入、update 恒 SKIPPED、delete 存在即删（W4 词表
    # 精化，2026-10-06）——钉死正确值 "create"，"insert" 会被 SKIPPED。
    assert 'op = "create"' in src, "terminal messages 的 update 必须 coerce 为 create 发射"
    assert 'op = "insert"' not in src


def test_sync_apply_append_only_accepts_create_only():
    """钉住对侧契约：messages 的 update 恒 SKIPPED（create 幂等插入、
    delete 存在即删）——非 create 的 update 形态一律 SKIPPED。"""
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "app", "services", "sync_apply.py"),
        encoding="utf-8").read()
    assert "APPEND_ONLY_TYPES" in src
    import re
    m = re.search(r'APPEND_ONLY_TYPES\s*=\s*[^)\n]*"messages"', src)
    assert m, "messages 必须在 APPEND_ONLY_TYPES"


# ── R6：打回回调 ──────────────────────────────────────────────────────────

def test_emit_draft_rejected_invokes_callback():
    from app.services.agent_loop import AgentLoop, AgentLoopState, _emit_draft_rejected, AuditVerdict
    loop = AgentLoop.__new__(AgentLoop)
    got = []

    async def cb(**kw):
        got.append(kw)

    loop.on_draft_rejected = cb
    state = AgentLoopState(messages=[])
    state.audit_rejected_drafts = [{"content": "草稿1"}]
    state.draft_attempt_seq = 1
    guidance = AuditVerdict(verdict="reject", problem="没有引用", source="llm")
    _run(_emit_draft_rejected(loop, state, "草稿1全文", guidance, "draft", attempt_no=1))
    assert got and got[0]["content"] == "草稿1全文"
    assert got[0]["verdict"] == "reject" and got[0]["problem"] == "没有引用"
    assert got[0]["attempt_no"] == 1


def test_stash_bumps_monotonic_attempt_seq():
    """A4.9 r1 M4：stash 饱和 pop-oldest 后 attempt_no 不得重复——单调计数器。"""
    from app.services.agent_loop import AgentLoopState, _stash_rejected_draft, AuditVerdict, _AUDIT_STASH_LIMIT
    state = AgentLoopState(messages=[])
    for _ in range(_AUDIT_STASH_LIMIT + 3):
        _stash_rejected_draft(state, "x", AuditVerdict(verdict="reject"), "draft")
    assert len(state.audit_rejected_drafts) == _AUDIT_STASH_LIMIT, "stash 有界"
    assert state.draft_attempt_seq == _AUDIT_STASH_LIMIT + 3, "计数器单调不随 pop 回退"


def test_emit_draft_rejected_fail_open():
    from app.services.agent_loop import AgentLoop, AgentLoopState, _emit_draft_rejected, AuditVerdict
    loop = AgentLoop.__new__(AgentLoop)

    async def bad_cb(**kw):
        raise RuntimeError("db down")

    loop.on_draft_rejected = bad_cb
    state = AgentLoopState(messages=[])
    state.audit_rejected_drafts = [{"content": "x"}]
    # 回调异常不得炸 loop
    _run(_emit_draft_rejected(loop, state, "x", AuditVerdict(verdict="reject"), "draft"))

    loop.on_draft_rejected = None
    _run(_emit_draft_rejected(loop, state, "x", AuditVerdict(verdict="reject"), "draft"))


def test_stash_sites_wired_to_emit():
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "app", "services", "agent_loop.py"),
        encoding="utf-8").read()
    n_stash = src.count("_stash_rejected_draft(") - 1  # 减 def 行
    n_emit = src.count("_emit_draft_rejected(self, state")
    assert n_emit >= n_stash >= 4, (
        f"每个 _stash_rejected_draft 调用点都要跟 _emit_draft_rejected（stash={n_stash}, emit={n_emit}）"
    )


# ── UPSTREAM_TODO_20261007 项1（P2，方案 D）：stub 外置 × sync_capture
#    快照期解析——messages 事件载荷必须携带全文（sha 校验通过），
#    孤儿/篡改保持桩 + warning；接收端零改动（本端重外置可展开）。──────────

def _payload_env():
    """SQLite in-memory + MessagePayload 表 + 真 before_flush 外置 hook。
    返回 (engine, conversation_id, user_id)。"""
    from app.db.database import (Base, Conversation, Message, MessagePayload,
                                 SyncEvent, SyncTombstone, User)
    from app.services.message_payload_service import install_message_payload_hook

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        User.__table__, Conversation.__table__, Message.__table__,
        MessagePayload.__table__, SyncEvent.__table__, SyncTombstone.__table__,
    ])
    with Session(engine) as s:
        u = User(username="pu", password_hash="x")
        s.add(u)
        s.flush()
        c = Conversation(user_id=u.id, title="t")
        s.add(c)
        s.commit()
        conv_id, user_id = c.id, u.id
    install_message_payload_hook()
    return engine, conv_id, user_id


def _messages_events(s):
    from app.db.database import SyncEvent
    return [e for e in s.execute(select(SyncEvent)).scalars().all()
            if e.entity_type == "messages"]


def test_sync_event_payload_resolves_externalized_full_text_on_insert():
    """项1 腿1：超限字段外置（before_flush hook）后，同 flush 发射的 create 事件
    载荷必须=全文（pending session 行解析 + sha 校验），而非孤儿桩。"""
    import app.services.sync_capture as sc
    from app.db.database import Message, MessagePayload
    from app.services.message_payload_service import uninstall_message_payload_hook

    engine, conv_id, _uid = _payload_env()
    sc.unregister_sync_capture()
    sc.register_sync_capture()
    big = "R" * 40000
    try:
        with Session(engine) as s, _sync_on():
            m = Message(conversation_id=conv_id, role="assistant", content="hi",
                        reasoning_content=big, delivery_status="final")
            s.add(m)
            s.commit()
            events = _messages_events(s)
            assert len(events) == 1, f"应恰 1 条 messages 事件，实得 {len(events)}"
            rc = (events[0].payload or {}).get("reasoning_content")
            assert rc == big, (
                "项1：事件载荷必须携带解析后的全文，实得 "
                f"{'stub' if isinstance(rc, str) else rc} len="
                f"{len(rc) if isinstance(rc, str) else 0}"
            )
            rows = s.execute(select(MessagePayload)).scalars().all()
            assert len(rows) == 1 and rows[0].content == big, "本端 payload 行仍应存全文"
    finally:
        sc.unregister_sync_capture()
        uninstall_message_payload_hook()


def test_sync_event_payload_resolves_full_text_on_update():
    """项1 腿2：字段更新（同 flush 重写既有 payload 行）后，update→create 事件
    载荷必须是**新值全文**（pending dirty 行优先于 DB 旧值）。"""
    import app.services.sync_capture as sc
    from app.db.database import Message
    from app.services.message_payload_service import uninstall_message_payload_hook

    engine, conv_id, _uid = _payload_env()
    sc.unregister_sync_capture()
    sc.register_sync_capture()
    try:
        with Session(engine) as s, _sync_on():
            m = Message(conversation_id=conv_id, role="assistant", content="hi",
                        reasoning_content="R" * 40000, delivery_status="final")
            s.add(m)
            s.commit()
            m2 = s.get(Message, m.id)
            big2 = "S" * 50000
            m2.reasoning_content = big2
            s.commit()
            latest = _messages_events(s)[-1]
            rc = (latest.payload or {}).get("reasoning_content")
            assert rc == big2, (
                "项1 更新路径：事件载荷必须携带新值全文（不得用 DB 旧值/桩），实得 "
                f"len={len(rc) if isinstance(rc, str) else rc}"
            )
    finally:
        sc.unregister_sync_capture()
        uninstall_message_payload_hook()


def test_sync_event_payload_resolves_tool_results_full_text():
    """项1 补腿（复审 B-3）：结构保持型外置（tool_results slim JSON 桩）同样解析
    为全文——`_EXTERNALIZED_FIELDS` 两个成员均在捕获面覆盖。"""
    import json as _json

    import app.services.sync_capture as sc
    from app.db.database import Message
    from app.services.message_payload_service import uninstall_message_payload_hook

    engine, conv_id, _uid = _payload_env()
    sc.unregister_sync_capture()
    sc.register_sync_capture()
    big_tool = _json.dumps(
        {"results": [{"title": "报告", "content": "C" * 40000}]}, ensure_ascii=False)
    try:
        with Session(engine) as s, _sync_on():
            m = Message(conversation_id=conv_id, role="assistant", content="hi",
                        tool_results=big_tool, delivery_status="final")
            s.add(m)
            s.commit()
            ev = _messages_events(s)[-1]
            got = (ev.payload or {}).get("tool_results")
            assert got == big_tool, (
                "tool_results 外置桩必须解析回全文（结构与字节同原文），实得 "
                f"len={len(got) if isinstance(got, str) else got}"
            )
    finally:
        sc.unregister_sync_capture()
        uninstall_message_payload_hook()


def test_sync_event_keeps_stub_when_orphan_or_tampered(caplog):
    """项1 腿3：孤儿桩（无 payload 行）与完整性不符（篡改 ref）一律保持桩 +
    warning——绝不外发未校验内容（与展开路由 404 同立场）。"""
    import json as _json
    import logging

    import app.services.sync_capture as sc
    from app.db.database import Message
    from app.services.message_payload_service import (compute_sha256,
                                                      uninstall_message_payload_hook)

    forged = _json.dumps({
        "_externalized": True, "field": "reasoning_content",
        "payload_ref": compute_sha256("attack"), "size_bytes": 6,
        "size_chars": 6, "preview": "attack",
    }, ensure_ascii=False)

    engine, conv_id, _uid = _payload_env()
    sc.unregister_sync_capture()
    sc.register_sync_capture()
    try:
        with Session(engine) as s, _sync_on():
            # 孤儿腿：行内即伪造桩（hook 见桩即跳过外置；无 payload 行）
            m1 = Message(conversation_id=conv_id, role="assistant", content="a",
                         reasoning_content=forged, delivery_status="final")
            s.add(m1)
            s.commit()
            ev1 = _messages_events(s)[-1]
            assert (ev1.payload or {}).get("reasoning_content") == forged, \
                "孤儿桩不得外发任何替换内容"
            # 篡改腿：真实 payload 行在场，但桩 ref 指向它内容
            m2 = Message(conversation_id=conv_id, role="assistant", content="b",
                         reasoning_content="T" * 40000, delivery_status="final")
            s.add(m2)
            s.commit()
            m2r = s.get(Message, m2.id)
            m2r.reasoning_content = forged  # 保留行，ref 与行内容不符
            with caplog.at_level(logging.WARNING, logger="app.services.sync_capture"):
                s.commit()
            ev2 = _messages_events(s)[-1]
            assert (ev2.payload or {}).get("reasoning_content") == forged, \
                "完整性不符时不得外发未校验内容（保持桩）"
            assert any("keeping stub" in r.message for r in caplog.records), \
                f"必须警示（实际日志 {[r.message for r in caplog.records]}）"
            # delete：无载荷、无需解析（不得炸）
            s.delete(m2r)
            s.commit()
            ev3 = _messages_events(s)[-1]
            assert ev3.op == "delete" and ev3.payload is None
    finally:
        sc.unregister_sync_capture()
        uninstall_message_payload_hook()


class _AsyncApplyShim:
    """apply_change 所需 AsyncSession 面的最小同步垫片（messages create 路径）。"""

    def __init__(self, s):
        self._s = s

    async def get(self, model, ident):
        return self._s.get(model, ident)

    def add(self, obj):
        self._s.add(obj)

    async def flush(self):
        self._s.flush()

    async def delete(self, obj):
        self._s.delete(obj)


def test_receiver_apply_reexternalizes_and_expands_full_text():
    """项1 腿4（接收侧零改动验证）：A 端事件载荷（全文）经 apply_change 落地 B 端
    → 本端 hook 重外置（行内本地签发桩 + 本地 payload 行全文）→ 归属寻址
    `_lookup_payload` 取回全文（展开面 200 语义）。"""
    import app.services.sync_capture as sc
    from app.db.database import Conversation, Message, User
    from app.services.message_payload_service import (compute_sha256, _lookup_payload,
                                                      is_externalized_stub,
                                                      stub_markers_trusted,
                                                      uninstall_message_payload_hook)
    from app.services.sync_apply import apply_change

    engine_a, conv_id, user_id = _payload_env()
    sc.unregister_sync_capture()
    sc.register_sync_capture()
    big = "A" * 40000
    try:
        with Session(engine_a) as sa, _sync_on():
            m = Message(conversation_id=conv_id, role="assistant", content="hi",
                        reasoning_content=big, delivery_status="final")
            sa.add(m)
            sa.commit()
            event = _messages_events(sa)[0]
            payload, msg_id = dict(event.payload), event.entity_id

        engine_b, _conv_b, _uid_b = _payload_env()
        with Session(engine_b) as sb, _sync_on():
            ub = User(id=user_id, username="pub", password_hash="x")
            sb.add(ub)
            sb.add(Conversation(id=conv_id, user_id=user_id, title="t"))
            sb.commit()
            res = _run(apply_change(_AsyncApplyShim(sb), ub, "messages", msg_id,
                                    "create", payload))
            assert res == "applied", f"对端 apply 必须成功，实得 {res}"
            row = sb.get(Message, msg_id)
            assert row is not None
            assert is_externalized_stub(row.reasoning_content), "对端必须本地重外置（行内=桩）"
            assert stub_markers_trusted(row.reasoning_content), "对端桩须为本地签发的严格形"
            ref = compute_sha256(big)
            local = _lookup_payload(sb, msg_id, "reasoning_content")
            assert local == big and compute_sha256(local) == ref, \
                "归属寻址展开必须取回全文（200 语义）"
    finally:
        sc.unregister_sync_capture()
        uninstall_message_payload_hook()
