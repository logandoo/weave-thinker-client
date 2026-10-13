"""TDD tests for the write-path externalization hook + read-path resolve (Task 2).

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_message_payload_hook.py -v
"""
import asyncio
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.database import Base, Message, MessagePayload
from app.services.message_payload_service import (
    build_field_stub,
    compute_sha256,
    is_externalized_stub,
    install_message_payload_hook,
    resolve_field,
    resolve_message_fields,
    should_externalize,
    uninstall_message_payload_hook,
)


@pytest.fixture(autouse=True)
def _clean_hook():
    """评审 M：hook 是全局 Session 监听——每测后卸载，防跨文件 flush 污染。"""
    yield
    uninstall_message_payload_hook()


def _fresh_session():
    engine = create_engine("sqlite:///:memory:")
    # SQLite can't render PostgreSQL JSONB columns in unrelated tables —
    # create only the tables this seam touches.
    Base.metadata.create_all(engine, tables=[Message.__table__, MessagePayload.__table__])
    return Session(engine)


class TestShouldExternalize:
    def test_under_limit_untouched(self):
        assert should_externalize("x" * 100, max_chars=1000) is False

    def test_over_limit(self):
        assert should_externalize("x" * 2000, max_chars=1000) is True

    def test_none_empty(self):
        assert should_externalize(None) is False
        assert should_externalize("") is False


class TestHook:
    def test_fat_message_externalized(self):
        install_message_payload_hook()
        sess = _fresh_session()
        fat = "r" * 50_000
        m = Message(id="m1", conversation_id="c1", role="assistant", content="ok",
                    reasoning_content=fat)
        sess.add(m)
        sess.commit()
        row = sess.get(Message, "m1")
        assert is_externalized_stub(row.reasoning_content)
        stub = json.loads(row.reasoning_content)
        assert stub["payload_ref"] == compute_sha256(fat)
        payloads = sess.execute(select(MessagePayload).where(MessagePayload.message_id == "m1")).scalars().all()
        assert len(payloads) == 1
        assert payloads[0].field == "reasoning_content"
        assert payloads[0].content == fat
        assert payloads[0].sha256 == compute_sha256(fat)
        sess.close()

    def test_thin_message_untouched(self):
        install_message_payload_hook()
        sess = _fresh_session()
        m = Message(id="m2", conversation_id="c1", role="assistant", content="ok",
                    reasoning_content="short", tool_results='{"a":1}')
        sess.add(m)
        sess.commit()
        row = sess.get(Message, "m2")
        assert row.reasoning_content == "short"
        assert row.tool_results == '{"a":1}'
        payloads = sess.execute(select(MessagePayload).where(MessagePayload.message_id == "m2")).scalars().all()
        assert len(payloads) == 0
        sess.close()

    def test_fat_tool_results_keeps_structure(self):
        install_message_payload_hook()
        sess = _fresh_session()
        big = json.dumps({"display_sequence": [
            {"type": "tool", "title": "工具调用 · workspace_read", "status": "completed",
             "result": "z" * 200_000}]}, ensure_ascii=False)
        m = Message(id="m3", conversation_id="c1", role="assistant", content="ok", tool_results=big)
        sess.add(m)
        sess.commit()
        row = sess.get(Message, "m3")
        obj = json.loads(row.tool_results)
        assert obj["_externalized"] is True
        assert obj["display_sequence"][0]["type"] == "tool"
        assert obj["display_sequence"][0]["__truncated__"] is True
        sess.close()

    def test_resolve_roundtrip_byte_identical(self):
        install_message_payload_hook()
        sess = _fresh_session()
        fat = "中" * 40_000 + "tail-marker-xyz"
        m = Message(id="m4", conversation_id="c1", role="assistant", content="ok",
                    reasoning_content=fat)
        sess.add(m)
        sess.commit()
        row = sess.get(Message, "m4")
        resolved = asyncio.run(resolve_field("m4", "reasoning_content", row.reasoning_content, sess))
        assert resolved == fat
        sess.close()

    def test_resolve_passthrough_for_inline(self):
        sess = _fresh_session()
        out = asyncio.run(resolve_field("m9", "reasoning_content", "plain", sess))
        assert out == "plain"
        sess.close()

    def test_resolve_message_fields_batch(self):
        install_message_payload_hook()
        sess = _fresh_session()
        fat_r = "R" * 40_000
        fat_t = json.dumps({"display_sequence": [{"type": "text", "content": "q" * 100_000}]})
        m = Message(id="m5", conversation_id="c1", role="assistant", content="ok",
                    reasoning_content=fat_r, tool_results=fat_t)
        sess.add(m)
        sess.commit()
        rows = [sess.get(Message, "m5")]
        out = asyncio.run(resolve_message_fields(rows, sess))
        assert out[0].reasoning_content == fat_r
        assert json.loads(out[0].tool_results)["display_sequence"][0]["content"] == "q" * 100_000
        sess.close()


class TestReviewRegressions:
    """A4.9 评审修复回归（C1 共享行级联删除 / C2 伪造桩换文）。"""

    def test_identical_content_not_shared_across_messages(self):
        """C1：同内容两消息各持有自己的 payload 行——删其一不毁其二。"""
        install_message_payload_hook()
        sess = _fresh_session()
        fat = "S" * 40_000
        m1 = Message(id="ma", conversation_id="c1", role="assistant", content="a", reasoning_content=fat)
        m2 = Message(id="mb", conversation_id="c1", role="assistant", content="b", reasoning_content=fat)
        sess.add_all([m1, m2])
        sess.commit()
        rows = sess.execute(select(MessagePayload)).scalars().all()
        assert len(rows) == 2  # 不共享
        sess.delete(sess.get(Message, "ma"))
        sess.commit()
        # mb 的 payload 存活
        alive = sess.execute(select(MessagePayload).where(MessagePayload.message_id == "mb")).scalars().all()
        assert len(alive) == 1 and alive[0].content == fat
        mb = sess.get(Message, "mb")
        resolved = asyncio.run(resolve_field("mb", "reasoning_content", mb.reasoning_content, sess))
        assert resolved == fat
        sess.close()

    def test_forged_stub_never_substitutes_foreign_content(self):
        """C2：伪造桩（无本消息 payload 行）→ 原样返回，不换入他文。"""
        sess = _fresh_session()
        forged = json.dumps({"_externalized": True, "field": "reasoning_content",
                             "payload_ref": compute_sha256("foreign"), "size_bytes": 7, "preview": "x"})
        out = asyncio.run(resolve_field("no-such-msg", "reasoning_content", forged, sess))
        assert out == forged  # 逐字节原样
        sess.close()

    def test_update_replaces_payload_inplace(self):
        """同消息字段重写：更新既有行而非新增（防孤儿增长）。"""
        install_message_payload_hook()
        sess = _fresh_session()
        m = Message(id="mc", conversation_id="c1", role="assistant", content="x", reasoning_content="A" * 40_000)
        sess.add(m)
        sess.commit()
        m.reasoning_content = "B" * 40_000
        sess.commit()
        rows = sess.execute(select(MessagePayload).where(MessagePayload.message_id == "mc")).scalars().all()
        assert len(rows) == 1 and rows[0].content == "B" * 40_000
        sess.close()
