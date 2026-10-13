"""reorder 端点同步时间戳契约（事故①修复，2026-10-02）。

事故：PUT /conversations/reorder 对同值 (sort_order, group_id) 的 ORM 同值赋值
会执行 UPDATE 并触发 sync_capture，但 SQLAlchemy 跳过 onupdate → 事件 payload
携带行旧 updated_at → push 后与云端行时间戳逐毫秒相等 → LWW `new_ts <=
current_ts`（sync_apply.py）判 STALE → skipped_remote ×24（「被云端仲裁拒绝」）。

契约（本文件钉死）：**reorder 对每个 item 都必须显式赋 `updated_at = utcnow()`**
——同值项也必须（这是既有漂移的自愈通道：重发同值 reorder 即把云端旧分组/排序
推平）；groups/reorder 同律。显式赋值同时压制 onupdate 缺位风险（任何 SQLAlchemy
行为变化下时间戳仍新鲜）。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_reorder_sync_ts.py -v
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.conversation import reorder_conversations, reorder_groups
from app.schemas.chat import (
    ConversationGroupReorderItem,
    ConversationGroupReorderRequest,
    ConversationReorderItem,
    ConversationReorderRequest,
)


def _fake_db(rows):
    """execute() 按 select 顺序依次返回 scalar_one_or_none() 行（None=未命中）。"""

    class _Result:
        def __init__(self, row):
            self._row = row

        def scalar_one_or_none(self):
            return self._row

    it = iter(rows)

    class _DB:
        async def execute(self, _q):
            return _Result(next(it, None))

        async def commit(self):
            return None

    return _DB()


def _run(coro):
    return asyncio.run(coro)


def _fresh(ts: datetime) -> bool:
    return ts is not None and ts > datetime.utcnow() - timedelta(seconds=30)


def test_reorder_bumps_updated_at_even_for_same_value_items():
    """同值 item（用户重发既有排列=漂移自愈通道）也必须拿到新鲜 updated_at。"""
    before = datetime(2026, 8, 4, 16, 58, 18, 706498)  # 事故现场：resync 落地的旧 ts
    conv = SimpleNamespace(
        id="a2eeff41-4c84-4710-9260-f9f6eb566402",
        user_id="u1",
        sort_order=7,
        group_id="6b5cd14c-ea85-4904-8cf8-eaa7a27cbbc9",
        updated_at=before,
    )
    db = _fake_db([conv])
    req = ConversationReorderRequest(items=[
        ConversationReorderItem(id=conv.id, sort_order=7, group_id=conv.group_id),
    ])
    _run(reorder_conversations(req, db=db, current_user=SimpleNamespace(id="u1")))
    assert conv.sort_order == 7 and conv.group_id == "6b5cd14c-ea85-4904-8cf8-eaa7a27cbbc9"
    assert _fresh(conv.updated_at), (
        f"同值 reorder 未显式赋新鲜 updated_at（got {conv.updated_at!r}）——"
        "事件 payload 将带旧 ts 被云端 LWW 等值判 STALE（事故①根因）"
    )


def test_reorder_bumps_updated_at_for_changed_items():
    conv = SimpleNamespace(id="c1", user_id="u1", sort_order=0, group_id=None,
                           updated_at=datetime(2026, 8, 4, 16, 58, 18))
    db = _fake_db([conv])
    req = ConversationReorderRequest(items=[
        ConversationReorderItem(id="c1", sort_order=3, group_id="g9"),
    ])
    _run(reorder_conversations(req, db=db, current_user=SimpleNamespace(id="u1")))
    assert conv.sort_order == 3 and conv.group_id == "g9"
    assert _fresh(conv.updated_at)


def test_group_reorder_bumps_updated_at_even_for_same_value_items():
    grp = SimpleNamespace(id="g1", user_id="u1", sort_order=2,
                          updated_at=datetime(2026, 8, 4, 16, 58, 18))
    db = _fake_db([grp])
    req = ConversationGroupReorderRequest(items=[
        ConversationGroupReorderItem(id="g1", sort_order=2),
    ])
    _run(reorder_groups(req, db=db, current_user=SimpleNamespace(id="u1")))
    assert grp.sort_order == 2
    assert _fresh(grp.updated_at), (
        f"groups/reorder 同值 item 未显式赋新鲜 updated_at（got {grp.updated_at!r}）"
    )


def test_group_reorder_bumps_updated_at_for_changed_items():
    grp = SimpleNamespace(id="g1", user_id="u1", sort_order=0,
                          updated_at=datetime(2026, 8, 4, 16, 58, 18))
    db = _fake_db([grp])
    req = ConversationGroupReorderRequest(items=[
        ConversationGroupReorderItem(id="g1", sort_order=5),
    ])
    _run(reorder_groups(req, db=db, current_user=SimpleNamespace(id="u1")))
    assert grp.sort_order == 5
    assert _fresh(grp.updated_at)


def test_reorder_skips_missing_rows_without_error():
    db = _fake_db([None])
    req = ConversationReorderRequest(items=[
        ConversationReorderItem(id="ghost", sort_order=1, group_id=None),
    ])
    _run(reorder_conversations(req, db=db, current_user=SimpleNamespace(id="u1")))
    # 不抛错即通过（幽灵 id 容忍语义与现行为一致）
