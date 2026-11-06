"""RED tests (A4.8) — P1 final 落库面不落「0 正文 + 仅 reasoning」（UPSTREAM_TODO_20261031，2026-10-31）。

事故（conv d7ea65b8 消息 f697cf2d，content="" reasoning=419 delivery_status=final）：
stop-cancel 分支门槛 `if _stop_content.strip() or _stop_tool_results or _stop_reasoning:`
只要 reasoning 非空就落 final——产生只有思考内容、没有正文的空气泡；legacy resume
`_completed` 守卫同病。sanitize（引用回声剥离/媒体本地化）在门槛之后执行，还可能把
正文清空后仍落 final。whitespace-only 的 streaming 行既删不掉（清道夫只认 ''/NULL）
又会被启动 sweep 翻成 interrupted（`if row.content:`），同样空气泡。

修复契约（工单 §3.2，语义=删除 reasoning-only 行，D-20261031-msgdel-p1-semantics）：
1. stop 门槛去 `or _stop_reasoning`：正文/工具结果才有 final；
2. sanitize/localize 之后、落库之前复检（清空→走空行清理）；
3. 迟到 stop（run.state.response_saved 已置位）不再落重复 final；
4. `_save_assistant_terminal` 支持 row_id 绑定本 run 行（stop 路径传
   streaming_message_id，防错删他人/旧 run 的行）；
5. `_delete_empty_streaming_row` 判据 trim 后为空；`mark_interrupted_streams`
   判据 `(content or "").strip()`；
6. legacy resume 守卫：无正文且无工具结果即跳过（reasoning 不参与）。

Run: cd backend && python -m pytest tests/test_stop_reasoning_only_p1.py -v
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine, event as sa_event, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

_BASE = datetime(2026, 10, 30, 22, 0, 0)


def _run(coro):
    return asyncio.run(coro)


class _Bridge:
    def __init__(self, s):
        self._s = s

    async def execute(self, stmt, *a, **k):
        return self._s.execute(stmt, *a, **k)

    async def commit(self):
        self._s.commit()

    async def rollback(self):
        self._s.rollback()

    async def delete(self, obj):
        self._s.delete(obj)

    def add(self, obj):
        self._s.add(obj)

    async def flush(self):
        self._s.flush()

    async def refresh(self, obj):
        self._s.refresh(obj)


class _SessionFactory:
    """替身 AsyncSessionLocal：每次 `async with AsyncSessionLocal()` 开新 sync Session。"""

    def __init__(self, engine):
        self.engine = engine

    def __call__(self):
        outer = self

        class _Ctx:
            async def __aenter__(self):
                self.s = Session(outer.engine)
                self.b = _Bridge(self.s)
                return self.b

            async def __aexit__(self, *exc):
                self.s.close()
                return False

        return _Ctx()


class _Buf:
    def __init__(self, streaming_message_id=None):
        self.streaming_message_id = streaming_message_id
        self.context_info = None
        self.completed = []

    async def mark_complete(self, mid):
        self.completed.append(mid)


class _AgentState:
    def __init__(self):
        self.is_running = True
        self.content = ""
        self.reasoning = ""
        self.done = 0

    async def broadcast_done(self):
        self.done += 1


def _mk_run(*, content="", reasoning="", tools=None, dm=None,
            row_id=None, response_saved=None, conv="c1"):
    buf = _Buf(streaming_message_id=row_id)
    state = SimpleNamespace(_stream_buf=buf, response_saved=response_saved,
                            _dm_terminal_status_saved=False)
    return SimpleNamespace(
        assistant_content=content,
        assistant_reasoning=reasoning,
        current_reasoning_segment="",
        content_segments=[],
        display_sequence=[],
        tool_results_accumulated=list(tools or []),
        search_queries_used=[],
        search_queries_by_call={},
        dm=dm,
        cap_conversation_id=conv,
        user_id_local="u1",
        username_local="user1",
        agent_state=_AgentState(),
        state=state,
    )


def _env(with_conv=True):
    from app.db.database import (
        Base, ChatSession, Conversation, ConversationGroup, Message, MessagePayload,
    )
    engine = create_engine("sqlite://", poolclass=StaticPool)

    @sa_event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    tables, stack, seen = [], [
        ConversationGroup.__table__, Conversation.__table__, Message.__table__,
        MessagePayload.__table__, ChatSession.__table__,
    ], set()
    while stack:
        t = stack.pop()
        if t.name in seen:
            continue
        seen.add(t.name)
        tables.append(t)
        for col in t.columns:
            for fk in col.foreign_keys:
                stack.append(fk.column.table)
    Base.metadata.create_all(engine, tables=tables)
    if with_conv:
        from app.db.database import User
        with Session(engine) as s:
            s.add(User(id="u1", username="user1", password_hash="x"))
            s.add(Conversation(id="c1", user_id="u1", title="t"))
            s.commit()
    return engine


def _add_msg(engine, mid, *, content="", reasoning=None, status="streaming",
             minutes=0, role="assistant", tool_results=None):
    from app.db.database import Message
    with Session(engine) as s:
        s.add(Message(id=mid, conversation_id="c1", role=role, content=content,
                      reasoning_content=reasoning, delivery_status=status,
                      tool_results=tool_results,
                      created_at=_BASE + timedelta(minutes=minutes)))
        s.commit()


def _rows(engine):
    from app.db.database import Message
    with Session(engine) as s:
        return {m.id: m for m in s.execute(select(Message)).scalars()}


def _patch(monkeypatch, engine, *, localize=None, dm_recorder=None):
    import app.api.chat_stream_transport as cts
    cts._sync_chat_globals()
    monkeypatch.setattr(cts, "AsyncSessionLocal", _SessionFactory(engine))
    if localize is not None:
        async def _loc(content, tr, uid, uname):
            return localize(content, tr)
        monkeypatch.setattr(cts, "_localize_media_for_persist", _loc)
    else:
        async def _loc(content, tr, uid, uname):
            return content, tr
        monkeypatch.setattr(cts, "_localize_media_for_persist", _loc)
    if dm_recorder is not None:
        async def _dm_fail(*a, **k):
            dm_recorder.append((a, k))
        monkeypatch.setattr(cts, "_persist_deathmatch_failure", _dm_fail)
    return cts


# ── 面 1：stop-cancel ─────────────────────────────────────────────────────

def test_stop_cancel_reasoning_only_deletes_streaming_row(monkeypatch):
    """事故本体：content="" reasoning>0 → 不落 final，streaming 行被删。"""
    engine = _env()
    _add_msg(engine, "s1", content="", reasoning=None)
    cts = _patch(monkeypatch, engine)
    run = _mk_run(reasoning="思考了 419 字但一个正文都没有", row_id="s1")
    _run(cts._handle_agent_loop_cancel(run))
    rows = _rows(engine)
    assert "s1" not in rows, f"reasoning-only 不得留下任何行，实得 {list(rows)}"


def test_stop_cancel_content_with_reasoning_persists(monkeypatch):
    """负控：正文非空仍保存，reasoning 一并落库（原地翻 final）。"""
    engine = _env()
    _add_msg(engine, "s1", content="已流出的")
    cts = _patch(monkeypatch, engine)
    run = _mk_run(content="部分回答", reasoning="思考", row_id="s1")
    _run(cts._handle_agent_loop_cancel(run))
    rows = _rows(engine)
    assert "s1" in rows and rows["s1"].delivery_status == "final"
    assert rows["s1"].content == "部分回答"
    assert rows["s1"].reasoning_content == "思考"


def test_stop_cancel_tool_results_only_persists(monkeypatch):
    """无正文但有工具结果 → 仍保存（工具结果不是 reasoning，保留原语义）。"""
    engine = _env()
    _add_msg(engine, "s1")
    cts = _patch(monkeypatch, engine)
    run = _mk_run(content="", reasoning="思考", tools=[{"name": "web_search", "ok": True}],
                  row_id="s1")
    _run(cts._handle_agent_loop_cancel(run))
    rows = _rows(engine)
    assert "s1" in rows and rows["s1"].delivery_status == "final"
    assert rows["s1"].tool_results, "工具结果必须落库"


def test_stop_cancel_whitespace_only_content_deletes_row(monkeypatch):
    """whitespace-only 正文 = 空：行被删（行 content='  \\n ' 也要删得掉）。"""
    engine = _env()
    _add_msg(engine, "s1", content="  \n ")
    cts = _patch(monkeypatch, engine)
    run = _mk_run(content="   ", reasoning="思考", row_id="s1")
    _run(cts._handle_agent_loop_cancel(run))
    assert "s1" not in _rows(engine), "whitespace-only 行必须被删除"


def test_stop_cancel_citation_echo_only_after_sanitize_not_final(monkeypatch):
    """门槛在 sanitize 前通过、sanitize/localize 把正文清空 → 复检后不落 final。"""
    engine = _env()
    _add_msg(engine, "s1", content="引用回声草稿（已被流给客户端）")
    cts = _patch(monkeypatch, engine, localize=lambda c, tr: ("", tr))
    run = _mk_run(content="引用回声草稿（已被流给客户端）", reasoning="思考", row_id="s1")
    _run(cts._handle_agent_loop_cancel(run))
    rows = _rows(engine)
    # 复检走空行清理：行要么被删（content 为空），绝不落 final
    assert not (rows.get("s1") and rows["s1"].delivery_status == "final"), \
        "sanitize 后正文清空仍落 final = 空气泡复发"


def test_stop_cancel_after_response_saved_no_duplicate(monkeypatch):
    """响应已由正常终态路径保存（response_saved）→ 迟到 stop 不再插入重复 final。"""
    engine = _env()
    _add_msg(engine, "f1", content="已保存的回答", status="final", minutes=-5)
    cts = _patch(monkeypatch, engine)
    run = _mk_run(content="迟到的 stop 又带一份", reasoning="思考", response_saved=True)
    _run(cts._handle_agent_loop_cancel(run))
    rows = _rows(engine)
    assert set(rows) == {"f1"}, f"迟到 stop 不得新增消息行，实得 {set(rows)}"


def test_stop_cancel_row_id_targets_own_row(monkeypatch):
    """row_id 绑定本 run 行：存在更晚的僵尸 streaming 行时，翻转的是本 run 的行。"""
    engine = _env()
    _add_msg(engine, "s0", content="旧 run 僵尸", minutes=100)   # 更晚创建
    _add_msg(engine, "s1", content="已流出的", minutes=0)         # 本 run
    cts = _patch(monkeypatch, engine)
    run = _mk_run(content="本 run 的回答", row_id="s1")
    _run(cts._handle_agent_loop_cancel(run))
    rows = _rows(engine)
    assert rows["s1"].delivery_status == "final", "必须翻本 run 的行"
    assert rows["s0"].delivery_status == "streaming", "不得动别的 run 的行"


def test_save_terminal_row_id_miss_inserts_new_row():
    """row_id 给定但未命中 streaming 行（已被终态路径翻掉/不存在）→ 插入新行，
    **不回头横扫**别的 run 的 streaming 行（回退「最新 streaming」会重新引入
    错翻他人行的 bug，见 _save_assistant_terminal docstring）。"""
    from app.api.chat_persistence import _save_assistant_terminal

    engine = _env()
    _add_msg(engine, "other", content="别的 run 在途", minutes=50)
    async def go():
        with Session(engine) as s:
            db = _Bridge(s)
            row = await _save_assistant_terminal(
                db, conversation_id="c1", content="迟到终稿",
                reasoning=None, tool_results=None, tool_calls=None,
                context_info=None, status="final", row_id="ghost-row")
            s.commit()
            return row.id
    rid = _run(go())
    rows = _rows(engine)
    assert rows[rid].delivery_status == "final"
    assert rows["other"].delivery_status == "streaming", "miss 分支不得横扫他 run 行"


def test_stop_cancel_dm_active_does_not_persist_empty_final(monkeypatch):
    """死磕 stop 分支：不走非 dm 落库面；失败落库交 _persist_deathmatch_failure。"""
    engine = _env()
    rec = []
    cts = _patch(monkeypatch, engine, dm_recorder=rec)
    run = _mk_run(reasoning="死磕中的思考", dm=object())
    _run(cts._handle_agent_loop_cancel(run))
    assert not _rows(engine), "dm 分支不得产生 final 行"
    assert rec, "dm 分支必须走 _persist_deathmatch_failure"


# ── 面 2：legacy resume 守卫（源码钉）─────────────────────────────────────

def test_legacy_resume_guard_excludes_reasoning():
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "api", "chat.py"),
               encoding="utf-8").read()
    assert 'if not (_cap_content or "").strip() and not _cap_tr_json:' in src, \
        "legacy resume 守卫必须是「无正文且无工具结果即跳过」（reasoning 不参与）"
    assert 'and not (_cap_reasoning or "").strip() and not _cap_tr_json' not in src, \
        "旧守卫（reasoning 参与落库判定）必须移除"


# ── 面 3：whitespace 判据 ─────────────────────────────────────────────────

def test_delete_empty_streaming_row_accepts_whitespace_only():
    from app.api.chat_persistence import _delete_empty_streaming_row

    engine = _env()
    _add_msg(engine, "s1", content="  \n\t")
    _add_msg(engine, "s2", content="有正文", minutes=1)

    async def go():
        with Session(engine) as s:
            db = _Bridge(s)
            ok1 = await _delete_empty_streaming_row(db, "s1")
            s.commit()
            ok2 = await _delete_empty_streaming_row(db, "s2")
            s.commit()
            return ok1, ok2

    ok1, ok2 = _run(go())
    assert ok1 is True, "whitespace-only streaming 行必须可删（trim 判据）"
    assert ok2 is False, "有正文的行绝不被删"
    rows = _rows(engine)
    assert "s1" not in rows and "s2" in rows


def test_mark_interrupted_streams_whitespace_deleted():
    from app.api.chat_persistence import mark_interrupted_streams

    engine = _env()
    _add_msg(engine, "s_ws", content="   ")
    _add_msg(engine, "s_txt", content="部分正文", minutes=1)

    async def go():
        with Session(engine) as s:
            db = _Bridge(s)
            flipped = await mark_interrupted_streams(db)
            await db.commit()
            return flipped

    flipped = _run(go())
    rows = _rows(engine)
    assert "s_ws" not in rows, "whitespace-only 空壳应直接删除，不是翻 interrupted"
    assert rows["s_txt"].delivery_status == "interrupted"
    assert flipped == 1


# ── 源码钉：stop 落库门槛不再含 reasoning ─────────────────────────────────

def test_stop_gate_no_reasoning_term():
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "api",
                            "chat_stream_transport.py"), encoding="utf-8").read()
    marker = "async def _handle_agent_loop_cancel("
    block = src[src.index(marker): src.index(marker) + 9000]
    assert "or _stop_reasoning" not in block, \
        "stop 落库门槛不得再包含 `or _stop_reasoning`（reasoning-only final 复发）"
