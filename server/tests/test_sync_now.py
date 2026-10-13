"""判据#4/#5 后端面（2026-09-30 用户追加准则）。

Task3: SyncClient 必须提供 wake()（asyncio.Event）——resync 端点重置游标后
立即唤醒周期，而非等 interval tick（判据#4「完成后刷新」的完成信号提前）。
Task4: POST /sync/account/sync-now —— 启动期轻量 delta 同步触发：已绑定
→ {scheduled:true} + wake；未绑定 → {scheduled:false} 200 不报错（判据#5 离线退路）。
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_sync_client_has_wake_event():
    from app.services.sync_client import SyncClient

    c = SyncClient()
    assert hasattr(c, "wake") and callable(c.wake), "SyncClient 必须有 wake()"
    c.wake()
    assert c._wake.is_set(), "wake() 必须置位 asyncio.Event（周期即时启动）"


def test_resync_endpoint_wakes_sync_client(monkeypatch):
    """resync 端点在重置游标后必须 wake()（不等 30s tick）。"""
    from app.services.sync_client import SyncClient

    fake_client = SyncClient()
    import app.api.sync as sync_api
    monkeypatch.setattr(sync_api, "sync_client", fake_client, raising=False)

    # 端点内部 `from app.services.sync_client import sync_client` 局部导入——
    # 补丁目标必须是源模块属性
    import app.services.sync_client as sc_mod
    monkeypatch.setattr(sc_mod, "sync_client", fake_client)

    row = SimpleNamespace(id="u1", sync_enabled=True, pull_cursor=5,
                          last_sync_at="2026-01-01T00:00:00", server_url="http://x",
                          cloud_username="cu", cloud_password="pw")

    class _Sess:
        async def get(self, model, key):
            return row

        async def commit(self):
            pass

    from app.api.sync import resync_sync_account
    body = asyncio.run(resync_sync_account(db=_Sess(), current_user=SimpleNamespace(id="u1")))

    assert body == {"resync": "scheduled"}
    assert row.pull_cursor == 0 and row.last_sync_at is None
    assert fake_client._wake.is_set(), "resync 后必须 wake()（判据#4 完成信号提前）"


def test_sync_now_unbound_returns_scheduled_false(monkeypatch):
    """判据#5：未绑定 → 200 {scheduled:false}（不抛 400，启动期零打扰）。"""
    from app.services.sync_client import SyncClient

    fake_client = SyncClient()
    import app.services.sync_client as sc_mod
    monkeypatch.setattr(sc_mod, "sync_client", fake_client)

    class _Sess:
        async def get(self, model, key):
            return None  # 未绑定

        async def commit(self):
            pass

    from app.api.sync import sync_now
    body = asyncio.run(sync_now(db=_Sess(), current_user=SimpleNamespace(id="u1")))
    assert body == {"scheduled": False}
    assert not fake_client._wake.is_set(), "未绑定不得唤醒周期"


def test_sync_now_bound_wakes(monkeypatch):
    from app.services.sync_client import SyncClient

    fake_client = SyncClient()
    import app.services.sync_client as sc_mod
    monkeypatch.setattr(sc_mod, "sync_client", fake_client)

    row = SimpleNamespace(id="u1", sync_enabled=True, server_url="http://x",
                          cloud_username="cu", cloud_password="pw")

    class _Sess:
        async def get(self, model, key):
            return row

        async def commit(self):
            pass

    from app.api.sync import sync_now
    body = asyncio.run(sync_now(db=_Sess(), current_user=SimpleNamespace(id="u1")))
    assert body == {"scheduled": True}
    assert fake_client._wake.is_set(), "已绑定必须 wake()（启动期同步立即跑）"


def test_resync_clears_stale_last_error(monkeypatch):
    """A4.9 评审 A#1：resync 必须清历史 last_error——否则前端把陈旧错误当成本次失败。"""
    from app.services.sync_client import SyncClient

    fake_client = SyncClient()
    import app.services.sync_client as sc_mod
    monkeypatch.setattr(sc_mod, "sync_client", fake_client)

    row = SimpleNamespace(id="u1", sync_enabled=True, pull_cursor=5,
                          last_sync_at="2026-01-01T00:00:00",
                          last_error="old failure", server_url="http://x",
                          cloud_username="cu", cloud_password="pw")

    class _Sess:
        async def get(self, model, key):
            return row

        async def commit(self):
            pass

    from app.api.sync import resync_sync_account
    asyncio.run(resync_sync_account(db=_Sess(), current_user=SimpleNamespace(id="u1")))

    assert row.last_error is None, "resync 必须清陈旧 last_error"
    assert fake_client.is_resync_pending("u1"), "resync 必须登记因果锚（B#2）"
    assert fake_client._wake.is_set()


def test_sync_now_half_bound_returns_false(monkeypatch):
    """A4.9 评审 B#11：绑定判定与 worker 对齐——缺 cloud_password 的半残行不得 scheduled:true。"""
    from app.services.sync_client import SyncClient

    fake_client = SyncClient()
    import app.services.sync_client as sc_mod
    monkeypatch.setattr(sc_mod, "sync_client", fake_client)

    row = SimpleNamespace(id="u1", sync_enabled=True, server_url="http://x",
                          cloud_username="cu", cloud_password=None)

    class _Sess:
        async def get(self, model, key):
            return row

        async def commit(self):
            pass

    from app.api.sync import sync_now
    body = asyncio.run(sync_now(db=_Sess(), current_user=SimpleNamespace(id="u1")))
    assert body == {"scheduled": False}, "半残绑定不得报 scheduled:true"
    assert not fake_client._wake.is_set(), "半残绑定不得唤醒周期"


def test_resync_pending_consumed_only_on_success():
    """B#2 因果锚语义：request 后 pending=true；discard 后 false（消费语义在 _cycle_one）。"""
    from app.services.sync_client import SyncClient

    c = SyncClient()
    assert c.is_resync_pending("u1") is False
    c.request_resync("u1")
    assert c.is_resync_pending("u1") is True
    c._resync_pending.discard("u1")  # 模拟 _resync 成功后 _cycle_one 的清除
    assert c.is_resync_pending("u1") is False


def test_sync_now_throttled(monkeypatch):
    """判据#8（A4.9 A#11/B#10）：per-user 5s 冷却——冷却内重复请求不放大 wake。"""
    import time as _time
    from app.services.sync_client import SyncClient

    fake_client = SyncClient()
    import app.services.sync_client as sc_mod
    monkeypatch.setattr(sc_mod, "sync_client", fake_client)

    row = SimpleNamespace(id="u1", sync_enabled=True, server_url="http://x",
                          cloud_username="cu", cloud_password="pw")

    class _Sess:
        async def get(self, model, key):
            return row

        async def commit(self):
            pass

    from app.api.sync import sync_now
    body1 = asyncio.run(sync_now(db=_Sess(), current_user=SimpleNamespace(id="u1")))
    assert body1 == {"scheduled": True}
    body2 = asyncio.run(sync_now(db=_Sess(), current_user=SimpleNamespace(id="u1")))
    assert body2 == {"scheduled": False, "throttled": True}, "冷却内必须节流"
    # 冷却窗口过后恢复
    fake_client._last_kick["u1"] = _time.monotonic() - 6
    body3 = asyncio.run(sync_now(db=_Sess(), current_user=SimpleNamespace(id="u1")))
    assert body3 == {"scheduled": True}


def test_resync_throttled(monkeypatch):
    """判据#8：resync 同样受限（防反复重置游标+wake 放大）。"""
    import time as _time
    from app.services.sync_client import SyncClient

    fake_client = SyncClient()
    import app.services.sync_client as sc_mod
    monkeypatch.setattr(sc_mod, "sync_client", fake_client)

    row = SimpleNamespace(id="u1", sync_enabled=True, pull_cursor=3,
                          last_sync_at="2026-01-01T00:00:00", last_error=None,
                          server_url="http://x", cloud_username="cu",
                          cloud_password="pw")

    class _Sess:
        async def get(self, model, key):
            return row

        async def commit(self):
            pass

    from app.api.sync import resync_sync_account
    asyncio.run(resync_sync_account(db=_Sess(), current_user=SimpleNamespace(id="u1")))
    try:
        asyncio.run(resync_sync_account(db=_Sess(), current_user=SimpleNamespace(id="u1")))
        raise AssertionError("冷却内 resync 必须 429")
    except Exception as e:
        assert getattr(e, "status_code", None) == 429, f"expect 429 got {e!r}"
    fake_client._last_kick["u1"] = _time.monotonic() - 6
    body = asyncio.run(resync_sync_account(db=_Sess(), current_user=SimpleNamespace(id="u1")))
    assert body == {"resync": "scheduled"}
