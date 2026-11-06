"""reorder 发射侧 ts bump 契约（2026-10-02 同步事故①回归钉）。

事故链（契约：memory/integration_weavethinker_sync_contract_20261002.md §3.1 ⚠；
审计：client 仓 docs/AUDIT_2026-10-02_sync_stall_and_arbitration_rejects.md §2）：
reorder 端点对同值 (sort_order, group_id) 的 ORM 同值赋值——SQLAlchemy 2.0.50
照常执行 UPDATE、照常触发 sync_capture after_update、但**跳过 onupdate** →
事件 payload 带旧 ts → 推送后与云端行 ts 逐毫秒相等 → LWW「严格大于」判 STALE
→ skipped_remote（生产实测 24 条 conversations + 1 条 note）。

契约钉死修法：**发射侧显式 `updated_at = datetime.utcnow()`**（对齐 chat.py 先例），
仲裁侧不放水（apply_change 双端同源不动）。

三层钉：
1. 陷阱实证（机制层）：同值赋值 → update 事件 payload 带旧 ts（为什么裸 rely
   onupdate 不够——事件会发但 ts 不前进）。
2. 载荷效应（机制层）：显式 bump → 事件 payload ts 前进（修法为什么有效）。
3. 源码契约层：两个 reorder 端点必须含显式 bump（防未来合并静默回退；与
   client 仓 server/tests/test_main_sync_wiring.py 同款守卫模式）。

Run: cd backend && ../.venv/bin/python -m pytest tests/test_reorder_emit_side_ts_bump.py -v
"""
import os
import re
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _render_jsonb_as_json_on_sqlite(element, compiler, **kw):
    """测试方言桥：SyncEvent.payload 在 PG 是 JSONB，sqlite 以 JSON 渲染（仅本测试进程）。"""
    return "JSON"


@compiles(BigInteger, "sqlite")
def _render_bigint_as_integer_on_sqlite(element, compiler, **kw):
    """sqlite 仅 INTEGER PRIMARY KEY 走 rowid 自增——BigInteger PK 渲染为 INTEGER。"""
    return "INTEGER"

from app.db.database import (
    Assistant,
    Conversation,
    ConversationGroup,
    SyncEvent,
    User,
    Base,
)
from app.services import sync_capture

OLD_TS = datetime(2026, 8, 4, 16, 58, 18, 706498)  # 生产事故 ts（2026-08-04T16:58:18.706498Z）


@pytest.fixture(scope="module", autouse=True)
def _capture_quarantine():
    """M1 升级（余留清零波）：本模块注册 capture，结束后 unregister_sync_capture()
    整体摘除监听器并复位幂等标志——后续测试模块可自行注册，互不污染。
    get_config 在模块内默认关、逐测试按需开。"""
    import app.core.config as config_mod
    from app.services import sync_capture as sc

    orig_get_config = config_mod.get_config
    config_mod.get_config = lambda: SimpleNamespace(
        sync_enabled=False, sync_memory_enabled=False
    )
    sc.register_sync_capture()  # 幂等；本模块负责注册
    yield
    sc.unregister_sync_capture()  # 监听器整体摘除（引用登记制）
    config_mod.get_config = orig_get_config


def _setup_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            Assistant.__table__,
            Conversation.__table__,
            ConversationGroup.__table__,
            SyncEvent.__table__,  # payload 列 JSONB 经下方 @compiles 以 JSON 渲染
        ],
    )
    return engine


def _enable_capture(monkeypatch):
    """_emit 发射期读 get_config()——替换为 sync_enabled=True 桩。"""
    import app.core.config as config_mod

    monkeypatch.setattr(
        config_mod,
        "get_config",
        lambda: SimpleNamespace(sync_enabled=True, sync_memory_enabled=False),
    )
    sync_capture.register_sync_capture()


def _last_update_payload(engine, entity_id):
    with engine.connect() as conn:
        rows = conn.execute(
            select(SyncEvent).where(
                SyncEvent.entity_id == entity_id, SyncEvent.op == "update"
            )
        ).fetchall()
    assert rows, "expected at least one update event"
    return rows[-1].payload


def _mk_conv(**kw):
    defaults = dict(
        id="conv-reorder-1",
        user_id="user-1",
        title="t",
        sort_order=7,
        group_id=None,
        created_at=OLD_TS,
        updated_at=OLD_TS,
    )
    defaults.update(kw)
    return Conversation(**defaults)


def test_same_value_assignment_emits_event_with_stale_ts(monkeypatch):
    """陷阱实证：同值赋值 → UPDATE 照发 + 事件照发 + payload ts 停留旧值。

    这就是生产 STALE×24 的机制：事件 payload.ts == 云端行 ts → LWW 严格大于判负。
    """
    _enable_capture(monkeypatch)
    engine = _setup_engine()
    with Session(engine) as s:
        s.add(_mk_conv())
        s.commit()

    with Session(engine) as s:
        conv = s.get(Conversation, "conv-reorder-1")
        conv.sort_order = 7   # 同值
        conv.group_id = None  # 同值
        s.commit()

    payload = _last_update_payload(engine, "conv-reorder-1")
    assert payload["updated_at"] == OLD_TS.isoformat() + "Z", (
        "同值赋值不应前进 payload ts——此断言钉住事故机制本身"
    )
    with Session(engine) as s:
        row = s.get(Conversation, "conv-reorder-1")
    assert row.updated_at == OLD_TS  # onupdate 被跳过（db ts 同样停留）


def test_explicit_bump_advances_event_payload_ts(monkeypatch):
    """载荷效应：同值赋值 + 发射侧显式 bump → payload ts 前进（修法有效性）。"""
    _enable_capture(monkeypatch)
    engine = _setup_engine()
    with Session(engine) as s:
        s.add(_mk_conv(id="conv-reorder-2"))
        s.commit()

    with Session(engine) as s:
        conv = s.get(Conversation, "conv-reorder-2")
        conv.sort_order = 7
        conv.group_id = None
        conv.updated_at = datetime.utcnow()  # ← 修复形态（与端点修复同款）
        s.commit()

    payload = _last_update_payload(engine, "conv-reorder-2")
    assert payload["updated_at"] != OLD_TS.isoformat() + "Z"
    with Session(engine) as s:
        row = s.get(Conversation, "conv-reorder-2")
    assert row.updated_at > OLD_TS + timedelta(days=1)


# ── 源码契约钉（合并守卫；RED 于修复前）────────────────────────────────

def _endpoint_src(marker: str) -> str:
    src = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "app", "api", "conversation.py"),
        encoding="utf-8",
    ).read()
    start = src.index(marker)
    end = src.index("@router", start + 10)
    return src[start:end]


def test_reorder_conversations_endpoint_bumps_updated_at():
    body = _endpoint_src("async def reorder_conversations")
    assert "conv.updated_at = datetime.utcnow()" in body, (
        "reorder_conversations 缺发射侧显式 ts bump——同值赋值事件 payload 带旧 ts，"
        "推送必被 LWW 等值判 STALE（事故①回归）"
    )


def test_reorder_groups_endpoint_bumps_updated_at():
    body = _endpoint_src("async def reorder_groups")
    assert "grp.updated_at = datetime.utcnow()" in body, (
        "reorder_groups 缺发射侧显式 ts bump（同事故①路径）"
    )


# ── I-1 收口钉：复审清单 9 处同类发射侧写点（10 端点）────────────────────
# 契约 §3.1 系统级不变量「真实变更必 bump updated_at」：
#   无条件赋值端点=直 bump；条件分支=set touched 才 bump；setattr 环=非空 update_data 才 bump；
#   循环批量=逐行 bump。空 PATCH（无字段命中）零事件零 bump。

_API_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")


def _fn_body(relpath: str, fn_name: str) -> str:
    src = open(os.path.join(_API_DIR, relpath), encoding="utf-8").read()
    m = re.search(rf"async def {fn_name}\(", src)
    assert m, f"{relpath} 缺函数 {fn_name}"
    nxt = src.find("@router", m.end())
    return src[m.start(): nxt if nxt != -1 else len(src)]


EMIT_SIDE_PINS = [
    ("api/conversation.py", "update_conversation",
     ["touched", "conversation.updated_at = datetime.utcnow()", "if touched:"]),
    ("api/conversation.py", "move_conversation",
     ["conversation.updated_at = datetime.utcnow()"]),
    ("api/conversation.py", "update_conversation_group",
     ["touched", "group.updated_at = datetime.utcnow()", "if touched:"]),
    ("api/conversation.py", "move_conversation_group",
     ["group.updated_at = datetime.utcnow()", "conv.updated_at = datetime.utcnow()"]),
    ("api/notes.py", "update_note",
     ["touched", "note.updated_at = datetime.utcnow()", "if touched:"]),
    ("api/notes.py", "update_notebook",
     ["notebook.updated_at = datetime.utcnow()"]),
    ("api/notes.py", "move_note",
     ["note.updated_at = datetime.utcnow()"]),
    ("api/notes.py", "bulk_move_notes",
     ["note.updated_at = datetime.utcnow()"]),
    ("api/assistant.py", "update_assistant",
     ["if update_data:", "assistant.updated_at = datetime.utcnow()"]),
    ("api/skills.py", "update_skill",
     ["touched", "skill.updated_at = datetime.utcnow()", "if touched:"]),
]

# 复审 A/B 合并裁定补钉：api/notes.py set_default_notebook（净零回环仍照发事件）
# + agent 工具写点（tools/ 下无 @router 边界，用文件级 needle）。
EXTRA_PINS = [
    ("api/notes.py", "set_default_notebook",
     ["notebook.updated_at = datetime.utcnow()"]),
]

TOOL_FILE_PINS = [
    ("tools/notes.py", ["note.updated_at = datetime.utcnow()", "if touched:"]),
    ("tools/skill_tools.py", ["skill.updated_at = datetime.utcnow()"]),
]


@pytest.mark.parametrize(
    "relpath,fn_name,needles", EMIT_SIDE_PINS + EXTRA_PINS,
    ids=[f"{f}:{n}" for f, n, _ in EMIT_SIDE_PINS + EXTRA_PINS],
)
def test_emit_side_write_points_bump_updated_at(relpath, fn_name, needles):
    body = _fn_body(relpath, fn_name)
    missing = [n for n in needles if n not in body]
    assert not missing, (
        f"{relpath}:{fn_name} 缺发射侧契约要素 {missing}——同值赋值 payload 带旧 ts，"
        "推送必被 LWW 等值判 STALE（事故①同类，契约 §3.1 系统级不变量）"
    )


@pytest.mark.parametrize(
    "relpath,needles", TOOL_FILE_PINS,
    ids=[f for f, _ in TOOL_FILE_PINS],
)
def test_tool_write_points_bump_updated_at(relpath, needles):
    """agent 工具写点（复审 B I-2）：无 @router 边界，文件级 needle 钉。"""
    src = open(os.path.join(_API_DIR, relpath), encoding="utf-8").read()
    missing = [n for n in needles if n not in src]
    assert not missing, (
        f"{relpath} 缺发射侧契约要素 {missing}（事故①同类）"
    )


# ── 余留项清零波：memory 域用户路径 ORM 化 + regenerate 守卫 + 反注册 ──────

def test_memory_forget_concept_orm_emits_capture():
    body = _fn_body("api/memory.py", "forget_concept")
    for needle in ("concept.status = \"forgotten\"", "concept.updated_at = datetime.utcnow()"):
        assert needle in body, f"forget_concept 缺 ORM 写点 {needle}"
    assert 'text("UPDATE memory_concepts' not in body, (
        "forget_concept 仍走 text() Core UPDATE——同步域零事件（捕获缺口残留）"
    )


def test_memory_delete_concept_orm_emits_capture():
    body = _fn_body("api/memory.py", "delete_concept")
    assert "await db.delete(concept)" in body, "delete_concept 应 ORM 删除（after_delete→事件+tombstone）"
    assert 'text("DELETE FROM memory_concepts' not in body, (
        "delete_concept 仍 text() 删同步域表——零事件零墓碑（捕获缺口残留）"
    )


def test_memory_delete_all_orm_for_synced_entities():
    body = _fn_body("api/memory.py", "delete_all_memory")
    assert body.count("await db.delete(obj)") >= 2, "concepts+episodes 两同步表需 ORM 逐行删除"
    for forbidden in ('text("DELETE FROM memory_concepts', 'text("DELETE FROM memory_episodes'):
        assert forbidden not in body, f"delete_all_memory 对同步域表仍用 text()：{forbidden}"


def test_clarification_memory_writes_orm():
    src = open(os.path.join(_API_DIR, "services/memory_clarification_service.py"), encoding="utf-8").read()
    assert "concept.valid_to = datetime.utcnow()" in src, "negate 分支需 ORM 化+bump"
    assert "await db.delete(concept)" in src, "forget 分支需 ORM 删除（事件+tombstone）——复审 C M-3 补钉"
    assert 'text("UPDATE memory_concepts' not in src, "negate 分支仍 text() UPDATE（捕获缺口）"
    assert 'text("DELETE FROM memory_concepts' not in src, "forget 分支仍 text() DELETE 同步域表"
    # 非同步域 junction 表保持 text()（无捕获语义，改动反而越界）
    assert 'text("DELETE FROM concept_cluster_members' in src
    assert 'text("DELETE FROM concept_relations' in src


def test_weight_service_cold_resurrect_orm():
    """复审 C I-1：try_cold_resurrect 用户召回内联可达——同步列写必须 ORM 化。"""
    src = open(
        os.path.join(_API_DIR, "services/memory_weight_service.py"), encoding="utf-8"
    ).read()
    assert "db.get(MemoryConcept, cid)" in src
    assert 'text("UPDATE memory_concepts SET status = \'active\'' not in src


def test_chat_regenerate_sort_order_value_guard():
    src = open(os.path.join(_API_DIR, "api/chat.py"), encoding="utf-8").read()
    src += open(os.path.join(_API_DIR, "api/chat_stream_transport.py"), encoding="utf-8").read()
    assert re.search(
        r"if conversation\.sort_order:\s*\n\s+conversation\.sort_order = 0", src
    ), "regenerate 应值守卫 sort_order=0（同值零事件，A4.9 R1 M-5+§3.1 双合规）"


def test_sync_capture_unregister_available():
    src = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "app", "services", "sync_capture.py"), encoding="utf-8"
    ).read()
    assert "def unregister_sync_capture" in src, "缺反注册——M1 升级（监听器引用保留）"
    assert "_LISTENERS" in src or "_REGISTERED_LISTENERS" in src
