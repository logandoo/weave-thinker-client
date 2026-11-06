"""REGRESSION (2026-10-22, 60 生产后台任务 3af90d5a 失败)：新 Message + 超限字段
（外置 MessagePayload）同 flush 时，`INSERT INTO message_payloads` 先于
`messages` → `message_payloads_message_id_fkey` 违规。

生产实证（2026-10-21 conv 5c89e092 / task 3af90d5a）：agent_worker 结果保存块
新 assistant Message(284KB tool_results) + Notebook 查询 autoflush →
IntegrityError: Key (message_id)=(653bc088-...) is not present in table
"messages"。真实 PG 复现+ revert-and-fail 双证据：tests/repro_worker_fk_20261022.py
（tests/wave60fix/fk_repro.log；修前 IntegrityError ↔ 修后 commit OK）。

根因（canonical SQLAlchemy 行为，SO 32913205 / zzzeek）：UOW 的 INSERT 依赖序
只由 relationship() 依赖处理器建立；表级 ForeignKeyConstraint 仅影响
create_all 排序。无 relationship 时 Message↔MessagePayload 相对次序是**进程
相关的任意 tie-break**：本 pytest 进程实测次序碰巧 messages 先行（revert 后行为
测试仍绿），评审 B 的 standalone 进程实测 children 先行（revert 后会红）——两种
观察都成立，正因如此行为测试**不能**作为确定性回归网，确定性网是本文件 T3 的
mapper 结构契约。生产进程当时落在 payload 先行一侧。

修复：Message↔MessagePayload relationship（passive_deletes=True——删除语义仍
交给 DB CASCADE，ORM 行为零变更；仅为建立确定性 UOW 依赖序）。

测试面：
- T1/T2 sqlite 行为烟（同形状端到端 + 修后 UOW 次序）——FK 即时检查与 PG 同；
- T3 结构契约（mapper relationship + passive_deletes）——revert-and-fail
  确定性回归网（无 relationship 时 sqlite 次序碰巧正确，行为测试无法钉住
  revert；结构契约可以）。
"""
import os
import sys

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_session():
    from app.db.database import Base
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    names = ("users", "conversation_groups", "assistants",
             "conversations", "messages", "message_payloads")
    Base.metadata.create_all(
        engine, tables=[Base.metadata.tables[n] for n in names])
    return sessionmaker(bind=engine)(), engine


@pytest.fixture()
def hook():
    from app.services.message_payload_service import (
        install_message_payload_hook, uninstall_message_payload_hook)
    uninstall_message_payload_hook()  # 既装兜底（跨文件测试卫生）
    install_message_payload_hook()
    yield
    uninstall_message_payload_hook()


def _seed_user_conv(db):
    import uuid
    from app.db.database import Conversation, User
    u = User(id=str(uuid.uuid4()), username="fk_t", password_hash="x")
    db.add(u)
    c = Conversation(id=str(uuid.uuid4()), user_id=u.id,
                     assistant_id=None, title="t")
    db.add(c)
    db.commit()
    return u.id, c.id


def test_new_message_with_externalized_payload_same_flush(hook):
    """agent_worker 保存块同形状：new Message(>32KB tool_results) + 查询触发
    autoflush——干净落库，字段外置，行内留 stub。"""
    import json
    import uuid
    from app.db.database import Message, MessagePayload, User

    db, engine = _make_session()
    _, conv_id = _seed_user_conv(db)

    big = json.dumps({"rounds": [], "results": [],
                      "agent_steps": [{"content": "y" * 40000}]},
                     ensure_ascii=False)
    msg = Message(id=str(uuid.uuid4()), conversation_id=conv_id,
                  role="assistant", content="z" * 100, tool_results=big)
    db.add(msg)
    db.query(User).first()  # 生产同款 Query-invoked autoflush
    db.commit()

    row = db.query(MessagePayload).filter_by(
        message_id=msg.id, field="tool_results").first()
    assert row is not None, "超限字段必须外置到 message_payloads"
    assert row.size_bytes == len(big.encode("utf-8"))
    persisted = db.get(Message, msg.id)
    assert persisted is not None
    assert persisted.tool_results != big, "行内应留 stub 而非全文"
    db.close()
    engine.dispose()


def test_uow_insert_order_messages_before_payloads(hook):
    """行为腿：修复后 INSERT messages 必须先于 INSERT message_payloads。"""
    import json
    import uuid
    from app.db.database import Message, User

    db, engine = _make_session()
    _, conv_id = _seed_user_conv(db)

    order = []

    @event.listens_for(engine, "before_cursor_execute")
    def _log(conn, cursor, statement, parameters, context, executemany):
        s = statement.strip().lower()
        if s.startswith("insert into messages") or s.startswith(
                "insert into message_payloads"):
            order.append(s.split("(")[0].strip())

    big = json.dumps({"agent_steps": [{"content": "y" * 40000}]})
    db.add(Message(id=str(uuid.uuid4()), conversation_id=conv_id,
                   role="assistant", content="z", tool_results=big))
    db.query(User).first()
    db.commit()

    # 稳健断言：只比较两表 INSERT 的相对次序（不依赖 seeding 之后无其他
    # messages INSERT 这一脆弱前提；re-review Minor-4）。
    assert "insert into messages" in order and "insert into message_payloads" in order, order
    assert order.index("insert into messages") < order.index(
        "insert into message_payloads"), f"UOW 依赖序错误（payload 先于 message）: {order}"
    db.close()
    engine.dispose()


def test_mapper_relationship_dependency_contract():
    """结构契约（revert-and-fail 确定性回归网）：Message↔MessagePayload 必须
    存在 relationship 以建立 UOW 依赖序，且 passive_deletes=True 保持 DB
    CASCADE 删除语义（契约注释见 database.py 两側）。"""
    from app.db.database import Message, MessagePayload

    m_rels = set(Message.__mapper__.relationships.keys())
    p_rels = set(MessagePayload.__mapper__.relationships.keys())
    assert "payloads" in m_rels, (
        "Message.payloads 缺失——无 relationship 时 UOW 次序为任意 tie-break，"
        "新 Message+外置 payload 同 flush 可 FK 违规（60 生产实证）")
    assert "message" in p_rels, "MessagePayload.message 缺失"
    assert Message.__mapper__.relationships["payloads"].passive_deletes is True
    assert MessagePayload.__mapper__.relationships["message"].passive_deletes is True


def test_orm_message_delete_still_cascades_via_db(hook):
    """删除语义不变性：ORM 删 Message → DB CASCADE 收走 payload（passive_deletes
    语义——ORM 不预加载/置空，约束层兜底）。"""
    import json
    import uuid
    from app.db.database import Message, MessagePayload

    db, engine = _make_session()
    _, conv_id = _seed_user_conv(db)
    big = json.dumps({"agent_steps": [{"content": "y" * 40000}]})
    msg = Message(id=str(uuid.uuid4()), conversation_id=conv_id,
                  role="assistant", content="z", tool_results=big)
    db.add(msg)
    db.commit()

    db.delete(msg)
    db.commit()
    assert db.query(MessagePayload).filter_by(message_id=msg.id).count() == 0
    db.close()
