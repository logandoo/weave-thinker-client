"""TDD — 遗留清偿波 fix-round I3：写-through drain 接停机。

shutdown 处 `await asyncio.wait_for(drain_write_through(), timeout=5)`（5s 超时
兜底）——优雅停机不得丢在飞写-through（B-8/D-15 落库面）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_shutdown_drain_write_through.py -v
"""
import asyncio
import inspect
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _stub_stops(monkeypatch, mainmod):
    async def _stop(*a, **k):
        return None

    for name in ("agent_scheduler", "agent_worker", "export_worker"):
        monkeypatch.setattr(mainmod, name, SimpleNamespace(stop=_stop))
    monkeypatch.setattr(mainmod, "shared_state", SimpleNamespace(is_db_enabled=False))

    async def _close():
        return None

    monkeypatch.setattr(mainmod, "close_shared_async_client", _close)


def test_shutdown_drains_write_through(monkeypatch):
    import main as mainmod
    import app.services.deathmatch_write_through as wt

    calls = []

    async def fake_drain():
        calls.append(1)

    monkeypatch.setattr(wt, "drain_write_through", fake_drain)
    _stub_stops(monkeypatch, mainmod)
    asyncio.run(mainmod.shutdown_event())
    assert calls == [1], "shutdown 必须 await drain_write_through()（在飞写-through 不得丢）"


def test_shutdown_drain_never_blocks_shutdown(monkeypatch):
    """5s 超时兜底：drain 挂死不得拖死停机。"""
    import main as mainmod
    import app.services.deathmatch_write_through as wt

    async def hanging_drain():
        await asyncio.sleep(600)

    monkeypatch.setattr(wt, "drain_write_through", hanging_drain)
    _stub_stops(monkeypatch, mainmod)
    src = inspect.getsource(mainmod.shutdown_event)
    assert "drain_write_through" in src
    assert "timeout=5" in src, "drain 必须带 5s 超时兜底（wait_for）"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
