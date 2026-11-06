"""TDD — 插话后回答排序（2026-11-01，conv a4d54ec7 现场报告）.

现象：用户在 run 进行中插话（interjection），插话消息在 run 中途落库；该轮终态
回答仍沿用 run 开始时创建的 assistant 行 created_at，因此 `order_by(created_at)`
把回答排在插话**之前** —— 前端最后一条是用户插话、下面没有任何回答，用户看到
「回答空了」（真实内容其实在上一条 assistant 气泡里）。

契约：
  O1 终态落库时，调用方把**本 run 已 commit 插话**的最大 created_at 作为
     `interjection_anchor` 传入；行 created_at 提到 anchor+1ms（原地更新不新增行）。
  O2 无 anchor（无插话 / buffer 不可用）→ created_at 原样不动——**绝不**用全会话
     user 消息 MAX 猜测（A4.9 I2：延迟终态会把本轮回答排到下一轮提问之后）。
  O3 anchor 不晚于行 created_at → 不动。
  O4 多次插话 → 取最大 anchor（最后一条插话之后）。
  O5 interrupted（stop/异常）终态同样适用。
  O6 只产生**一次** after_update 事件且携带最终时间戳（A4.9 I1：autoflush 先刷旧值
     会让跨设备同步停在错误顺序）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_interjection_message_ordering.py -v
"""
import asyncio
from datetime import datetime, timedelta

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.api.chat_persistence import _save_assistant_terminal, interjection_anchor_from_buffer
from app.db.database import Base, Conversation, Message, User


class _Bridge:
    """asyncio 鸭子壳包真实 sync Session（SQLite 内存）——同 test_trim_purges_memory。"""

    def __init__(self, sync_sess):
        self._s = sync_sess

    async def execute(self, stmt):
        return self._s.execute(stmt)

    async def commit(self):
        self._s.commit()


def _run(coro):
    return asyncio.run(coro)


def _sqlite_tables():
    """只建本测试用到的表（FK 传递闭包）——全量 metadata 含 JSONB，SQLite 渲染不了。"""
    tables, stack, seen = [], [Conversation.__table__, Message.__table__], set()
    while stack:
        t = stack.pop()
        if t.name in seen:
            continue
        seen.add(t.name)
        tables.append(t)
        for col in t.columns:
            for fk in col.foreign_keys:
                stack.append(fk.column.table)
    return tables


def _session_with(turn_start: datetime, interjection_at: datetime | None):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=_sqlite_tables())
    sess = Session(engine)
    sess.add(User(id="u1", username="t", password_hash="x"))
    sess.add(Conversation(id="c1", user_id="u1", title="t"))
    sess.add(Message(
        id="m-assistant", conversation_id="c1", role="assistant",
        content="", delivery_status="streaming", created_at=turn_start,
        reasoning_content=None,
    ))
    if interjection_at is not None:
        sess.add(Message(
            id="m-user-interject", conversation_id="c1", role="user",
            content="插话内容", delivery_status="final", created_at=interjection_at,
        ))
    sess.commit()
    return sess


def _finalize(sess, *, anchor=None, status="final"):
    return _run(_save_assistant_terminal(
        _Bridge(sess), conversation_id="c1", content="最终回答",
        reasoning=None, tool_results=None, tool_calls=None,
        context_info=None, status=status, interjection_anchor=anchor,
    ))


def test_final_answer_sorts_after_interjection():
    """O1：anchor 晚于 assistant 行 → 终态行排到插话之后，且原地更新。"""
    t0 = datetime(2026, 11, 1, 8, 8, 8)
    interjection = datetime(2026, 11, 1, 8, 9, 29)
    sess = _session_with(t0, interjection)
    _finalize(sess, anchor=interjection)
    sess.commit()
    rows = sess.execute(
        select(Message).where(Message.conversation_id == "c1").order_by(Message.created_at)
    ).scalars().all()
    assert [r.role for r in rows] == ["user", "assistant"], "助手行必须排在插话之后"
    assert rows[-1].id == "m-assistant" and rows[-1].content == "最终回答"
    assert rows[-1].created_at == interjection + timedelta(milliseconds=1)


def test_no_anchor_never_moves_row():
    """O2：无 anchor（无插话 / buffer 不可用）→ 即使存在更晚的 user 行也不动。

    A4.9 I2：全会话 MAX 会把延迟终态排到下一轮提问之后。"""
    t0 = datetime(2026, 11, 1, 8, 8, 8)
    later_question = datetime(2026, 11, 1, 8, 10, 0)
    sess = _session_with(t0, later_question)
    _finalize(sess, anchor=None)
    sess.commit()
    row = sess.execute(select(Message).where(Message.id == "m-assistant")).scalar_one()
    assert row.created_at == t0


def test_older_anchor_does_not_move_row():
    """O3：anchor 早于行 created_at → 不动。"""
    t0 = datetime(2026, 11, 1, 8, 8, 8)
    sess = _session_with(t0, None)
    _finalize(sess, anchor=t0 - timedelta(seconds=5))
    sess.commit()
    row = sess.execute(select(Message).where(Message.id == "m-assistant")).scalar_one()
    assert row.created_at == t0


def test_multiple_interjections_use_latest():
    """O4：多次插话取最大 anchor。"""
    t0 = datetime(2026, 11, 1, 8, 8, 8)
    first = datetime(2026, 11, 1, 8, 9, 0)
    second = datetime(2026, 11, 1, 8, 9, 30)
    sess = _session_with(t0, first)
    sess.add(Message(id="m-ij2", conversation_id="c1", role="user",
                     content="第二次插话", delivery_status="final", created_at=second))
    sess.commit()
    _finalize(sess, anchor=second)
    sess.commit()
    row = sess.execute(select(Message).where(Message.id == "m-assistant")).scalar_one()
    assert row.created_at > second


def test_interrupted_partial_also_sorts_after_interjection():
    """O5：中断终态同样适用。"""
    t0 = datetime(2026, 11, 1, 8, 8, 8)
    interjection = t0 + timedelta(seconds=30)
    sess = _session_with(t0, interjection)
    _finalize(sess, anchor=interjection, status="interrupted")
    sess.commit()
    row = sess.execute(select(Message).where(Message.id == "m-assistant")).scalar_one()
    assert row.created_at > interjection
    assert row.delivery_status == "interrupted"
    assert row.content == "最终回答"


def test_single_sync_update_event_with_final_timestamp():
    """O6：只有一次 after_update 事件，且携带最终时间戳（autoflush 回归钉）。"""
    t0 = datetime(2026, 11, 1, 8, 8, 8)
    interjection = datetime(2026, 11, 1, 8, 9, 29)
    sess = _session_with(t0, interjection)
    # 强引用：identity_map 是弱引用，GC 后监听器看不到对象
    keep_alive = sess.execute(select(Message).where(Message.id == "m-assistant")).scalar_one()
    seen: list = []

    def _capture(s, flush_context=None):
        if s is not sess:
            return
        seen.extend(
            (obj.created_at, obj.delivery_status)
            for obj in s.identity_map.values()
            if isinstance(obj, Message) and obj.id == "m-assistant"
        )

    # after_flush_postexec：每次 flush 记一次。autoflush 回归时第一次 flush 携带
    # 旧 created_at（跨设备 append-only 丢弃后一次），这里会看到 2 条。
    event.listen(Session, "after_flush_postexec", _capture)
    try:
        _finalize(sess, anchor=interjection)
        sess.commit()
    finally:
        event.remove(Session, "after_flush_postexec", _capture)
    assert keep_alive is not None
    assert len(seen) == 1, f"必须只有一次 update 事件（实际 {len(seen)}：{seen}）"
    assert seen[0][0] == interjection + timedelta(milliseconds=1)


def test_anchor_from_buffer_uses_max_iso_timestamp():
    """anchor 提取：buffer 回执里 created_at 的最大值；坏值/缺值忽略。"""
    class _Buf:
        interjections_committed = [
            {"created_at": "2026-11-01T08:09:00"},
            {"created_at": None},
            {"created_at": "not-a-date"},
            {"created_at": "2026-11-01T08:09:30.500000"},
        ]

    assert interjection_anchor_from_buffer(_Buf()) == datetime(2026, 11, 1, 8, 9, 30, 500000)
    assert interjection_anchor_from_buffer(None) is None


def test_every_terminal_save_in_transport_binds_anchor_and_row_id():
    """源码级钉子（A4.9 NB-3）：transport 里每一处终态落库都必须传 anchor + row_id。

    漏传一处 = 该路径（断连补存/中断保存/旧消费端）退回原症状或反查僵尸行。
    """
    import inspect
    import re

    from app.api import chat_stream_transport as transport

    blocks = re.findall(
        r"_save_assistant_terminal\(\n(.*?)\n\s*\)\n",
        inspect.getsource(transport),
        re.S,
    )
    assert len(blocks) == 6, f"transport 终态落库调用点应为 6 处（实际 {len(blocks)}）"
    for idx, block in enumerate(blocks):
        assert "interjection_anchor=" in block, f"第 {idx + 1} 处调用未传 interjection_anchor"
        assert "row_id=" in block, f"第 {idx + 1} 处调用未传 row_id"
