"""TDD: 助手消息 context_info 持久化链（P2 2026-09-05）。

问题：context_info（本轮 token 实测/估算）只活在内存 StreamBuffer + 发送端
localStorage——手机端发出的轮次在电脑端打开看不到任何 token 信息（无快照不显示
是 P4 徽章真值波的既定语义）。修复：messages 表加 context_info 列，生产者
persist 落账，消息接口透出，前端加载期播种。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_message_context_info.py -v
"""
import inspect

from app.db.database import Message
from app.db.migrations import STARTUP_MIGRATIONS
from app.schemas.chat import MessageResponse


def test_message_model_has_context_info_column():
    assert "context_info" in Message.__table__.columns, (
        "messages 表缺 context_info 列（token 用量无法持久化）"
    )
    assert Message.__table__.columns["context_info"].nullable


def test_startup_migration_registered():
    versions = [v for v, _ in STARTUP_MIGRATIONS]
    assert "messages_context_info" in versions, "STARTUP_MIGRATIONS 缺 messages_context_info"
    stmt = dict(STARTUP_MIGRATIONS)["messages_context_info"]
    assert "ADD COLUMN IF NOT EXISTS" in stmt  # 幂等，重复启动安全


def test_message_response_schema_has_field():
    assert "context_info" in MessageResponse.model_fields
    m = MessageResponse(
        id="m1", conversation_id="c1", role="assistant", content="x",
        context_info='{"tokens": 123, "context_length": 262144, "measured": true}',
        created_at="2026-09-05T00:00:00",
    )
    assert m.context_info and "123" in m.context_info


def test_conversation_routes_serialize_context_info():
    """两处消息序列化（GET /{id} 与 GET /{id}/messages）都必须透出。"""
    import app.api.conversation as conv_mod

    src = inspect.getsource(conv_mod)
    hits = src.count("context_info=m.context_info")
    assert hits >= 2, f"conversation.py 仅 {hits} 处序列化 context_info（需 2 处）"
