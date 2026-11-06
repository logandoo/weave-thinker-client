"""2026-11-03: browser session / video recorder lifecycle tests.

Root cause of the 60-box incident: ``record_video_dir`` is started at context
creation and Playwright only stops encoding on ``context.close()``. The chat
turn ended at 00:20 but the session (idle timeout 18000 s) kept the Chromium +
ffmpeg tree alive, burning ~1.3 GB RSS and ~260 MB/s reads.

Run from backend/:  python -m pytest tests/test_browser_session_lifecycle.py -v
"""
import asyncio
import os
import time

import pytest

import app.services.interactive_browser_service as ibs
from app.services.interactive_browser_service import BrowserSession, InteractiveBrowserService


class _FakeHandle:
    def __init__(self, fail: bool = False):
        self.closed = False
        self.fail = fail

    async def close(self):
        if self.fail:
            raise RuntimeError("close failed")
        self.closed = True


class _FakeContext(_FakeHandle):
    async def storage_state(self, path):
        return None


class _FakeCfg:
    browser_interaction_close_on_turn_end = True
    browser_interaction_max_lifetime = 1800
    browser_interaction_session_timeout = 300
    browser_interaction_max_concurrent = 5


def _session(conv: str, video_dir: str = "", **kwargs) -> BrowserSession:
    return BrowserSession(
        session_id=conv,
        browser=_FakeHandle(**kwargs),
        context=_FakeContext(**kwargs),
        page=_FakeHandle(**kwargs),
        video_dir=video_dir,
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(ibs, "config", _FakeCfg())
    # never write storage_state into the repo during tests
    monkeypatch.setattr(ibs, "_storage_state_path", lambda _cid: "")


def test_close_session_closes_handles_and_removes_video_dir(tmp_path):
    video_dir = tmp_path / "vid"
    video_dir.mkdir()
    (video_dir / "page.webm").write_bytes(b"x")

    svc = InteractiveBrowserService()
    sess = _session("c1", video_dir=str(video_dir))
    svc._sessions["c1"] = sess

    assert asyncio.run(svc.close_session("c1", reason="test")) is True
    assert "c1" not in svc._sessions
    assert sess.page.closed and sess.context.closed and sess.browser.closed
    assert not video_dir.exists()


def test_close_session_is_idempotent_for_unknown_conversation():
    svc = InteractiveBrowserService()
    assert asyncio.run(svc.close_session("missing")) is False


def test_close_session_survives_failing_handles(tmp_path):
    svc = InteractiveBrowserService()
    sess = _session("c1", video_dir="", fail=True)
    svc._sessions["c1"] = sess

    assert asyncio.run(svc.close_session("c1")) is True
    assert "c1" not in svc._sessions


def test_close_for_turn_end_respects_config_flag(monkeypatch):
    class _Off(_FakeCfg):
        browser_interaction_close_on_turn_end = False

    monkeypatch.setattr(ibs, "config", _Off())
    svc = InteractiveBrowserService()
    sess = _session("c1")
    svc._sessions["c1"] = sess

    assert asyncio.run(svc.close_for_turn_end("c1")) is False
    assert "c1" in svc._sessions
    assert not sess.page.closed


def test_close_for_turn_end_closes_session():
    svc = InteractiveBrowserService()
    sess = _session("c1")
    svc._sessions["c1"] = sess

    assert asyncio.run(svc.close_for_turn_end("c1")) is True
    assert "c1" not in svc._sessions
    assert sess.context.closed


def test_cleanup_expired_closes_idle_session(monkeypatch):
    class _Cfg(_FakeCfg):
        browser_interaction_session_timeout = 60
        browser_interaction_max_lifetime = 0

    monkeypatch.setattr(ibs, "config", _Cfg())
    svc = InteractiveBrowserService()
    sess = _session("idle")
    sess.last_activity = time.time() - 120
    svc._sessions["idle"] = sess

    asyncio.run(svc._cleanup_expired())
    assert "idle" not in svc._sessions


def test_cleanup_expired_closes_over_lifetime_session(monkeypatch):
    class _Cfg(_FakeCfg):
        browser_interaction_session_timeout = 18000
        browser_interaction_max_lifetime = 1800

    monkeypatch.setattr(ibs, "config", _Cfg())
    svc = InteractiveBrowserService()
    sess = _session("long")
    sess.last_activity = time.time() - 600    # idle past the 300s grace
    sess.created_at = time.time() - 3600      # and past the hard cap
    svc._sessions["long"] = sess

    asyncio.run(svc._cleanup_expired())
    assert "long" not in svc._sessions


def test_cleanup_keeps_fresh_session(monkeypatch):
    monkeypatch.setattr(ibs, "config", _FakeCfg())
    svc = InteractiveBrowserService()
    svc._sessions["fresh"] = _session("fresh")

    asyncio.run(svc._cleanup_expired())
    assert "fresh" in svc._sessions


def test_schedule_browser_close_invokes_service(monkeypatch):
    import app.api.chat_stream_transport as transport

    calls = []

    async def _fake_close(self, conversation_id, turn_token=None):
        calls.append((conversation_id, turn_token))
        return True

    monkeypatch.setattr(
        InteractiveBrowserService, "close_for_turn_end", _fake_close, raising=True
    )

    async def _run():
        transport._schedule_browser_close("conv-x", "turn:abc")
        await asyncio.sleep(0.05)

    asyncio.run(_run())
    assert calls == [("conv-x", "turn:abc")]


def test_close_for_turn_end_ignores_other_turn_token():
    svc = InteractiveBrowserService()
    sess = _session("c1")
    sess.turn_token = "turn:new"
    svc._sessions["c1"] = sess

    assert asyncio.run(svc.close_for_turn_end("c1", turn_token="turn:old")) is False
    assert "c1" in svc._sessions
    assert not sess.page.closed


def test_close_for_turn_end_never_steals_background_session():
    """turn_token=None (background-owned) must not be closed by a chat turn."""
    svc = InteractiveBrowserService()
    sess = _session("c1")
    sess.turn_token = None
    svc._sessions["c1"] = sess

    assert asyncio.run(svc.close_for_turn_end("c1", turn_token="turn:x")) is False
    assert "c1" in svc._sessions


def test_close_session_expected_identity_guard():
    svc = InteractiveBrowserService()
    old = _session("c1")
    new = _session("c1")
    svc._sessions["c1"] = new

    assert asyncio.run(svc.close_session("c1", expected=old)) is False
    assert "c1" in svc._sessions
    assert not new.page.closed


def test_close_session_calls_handles_in_order(monkeypatch):
    calls = []

    class _OrderedContext(_FakeHandle):
        async def storage_state(self, path):
            calls.append("storage_state")

        async def close(self):
            calls.append("context")
            await super().close()

    class _OrderedPage(_FakeHandle):
        async def close(self):
            calls.append("page")
            await super().close()

    class _OrderedBrowser(_FakeHandle):
        async def close(self):
            calls.append("browser")
            await super().close()

    svc = InteractiveBrowserService()
    sess = BrowserSession(
        session_id="c1",
        browser=_OrderedBrowser(),
        context=_OrderedContext(),
        page=_OrderedPage(),
    )
    svc._sessions["c1"] = sess
    monkeypatch.setattr(ibs, "_storage_state_path", lambda _cid: "/tmp/state.json")
    monkeypatch.setattr("os.chmod", lambda *a, **k: None)

    asyncio.run(svc.close_session("c1"))
    assert calls == ["storage_state", "page", "context", "browser"]


def test_close_session_timeout_is_bounded_and_still_cleans(tmp_path, monkeypatch):
    class _SlowHandle(_FakeHandle):
        async def close(self):
            await asyncio.sleep(10)

    video_dir = tmp_path / "vid"
    video_dir.mkdir()
    svc = InteractiveBrowserService()
    sess = BrowserSession(
        session_id="c1",
        browser=_SlowHandle(),
        context=_SlowHandle(),
        page=_SlowHandle(),
        video_dir=str(video_dir),
    )
    svc._sessions["c1"] = sess
    monkeypatch.setattr(ibs, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def _run():
        t0 = asyncio.get_event_loop().time()
        closed = await svc.close_session("c1")
        return closed, asyncio.get_event_loop().time() - t0

    closed, elapsed = asyncio.run(_run())
    assert closed is True
    assert elapsed < 1.0
    assert "c1" not in svc._sessions
    assert not video_dir.exists()


def test_cleanup_max_lifetime_zero_keeps_fresh(monkeypatch):
    class _Cfg(_FakeCfg):
        browser_interaction_session_timeout = 18000
        browser_interaction_max_lifetime = 0

    monkeypatch.setattr(ibs, "config", _Cfg())
    svc = InteractiveBrowserService()
    sess = _session("fresh")
    sess.last_activity = time.time()
    sess.created_at = time.time() - 10 * 3600
    svc._sessions["fresh"] = sess

    asyncio.run(svc._cleanup_expired())
    assert "fresh" in svc._sessions


def test_cleanup_does_not_kill_actively_used_long_session(monkeypatch):
    """Hard lifetime only applies after an idle grace period."""
    class _Cfg(_FakeCfg):
        browser_interaction_session_timeout = 18000
        browser_interaction_max_lifetime = 1800

    monkeypatch.setattr(ibs, "config", _Cfg())
    svc = InteractiveBrowserService()
    sess = _session("busy")
    sess.created_at = time.time() - 3600
    sess.last_activity = time.time()          # used seconds ago
    svc._sessions["busy"] = sess

    asyncio.run(svc._cleanup_expired())
    assert "busy" in svc._sessions


def test_sweep_stale_video_dirs_removes_only_old(tmp_path, monkeypatch):
    old = tmp_path / "wt_browser_video_old_dead"
    new = tmp_path / "wt_browser_video_new_live"
    old.mkdir()
    new.mkdir()
    old_time = time.time() - (48 * 3600)
    os.utime(old, (old_time, old_time))

    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    removed = ibs._sweep_stale_video_dirs(max_age_seconds=24 * 3600)

    assert removed == 1
    assert not old.exists()
    assert new.exists()
