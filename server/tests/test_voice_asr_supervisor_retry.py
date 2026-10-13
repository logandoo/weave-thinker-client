"""TDD tests — ASR supervisor must survive >5 transient failures (2026-09-20).

用户报告「你能听到我说话吗」类 query 完全被吞（无识别）+ 界面红色。
`_funasr_supervisor` 旧行为：重连失败累计 5 次即 emit error 并 **return** ——
此后本会话永久失聪（DashScope 瞬断/短时不可用也会触发），只能断开重连会话。
会话仍持麦克风时，持续重连（cap 退避）远优于永久死亡；上游恢复后 `task-started`
→ ready 自动回到 listen。error 提示仍会发一次（用户可见），但不再终止。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_voice_asr_supervisor_retry.py -v
"""
import asyncio

from app.services import voice_service
from app.services.asr_service import ASRService
from app.services.voice_service import _VoiceASR


class _FastAsyncio:
    """Shim: 0.5*N backoff sleeps collapse to 10ms (test speed) — everything
    else delegates to the real asyncio module."""

    FIRST_COMPLETED = asyncio.FIRST_COMPLETED

    @staticmethod
    async def sleep(_seconds):
        await asyncio.sleep(0.01)

    @staticmethod
    def create_task(coro):
        return asyncio.ensure_future(coro)

    @staticmethod
    async def wait(tasks, return_when=None):
        return await asyncio.wait(tasks, return_when=return_when)

    @staticmethod
    async def gather(*args, **kwargs):
        return await asyncio.gather(*args, **kwargs)


def test_supervisor_retries_first_connect_failure(monkeypatch):
    """A transient failure on the FIRST connect must not kill recognition
    permanently (A4.9 review Important-2)."""

    async def run():
        queue = asyncio.Queue()
        asr = _VoiceASR(ASRService(), queue)

        monkeypatch.setattr(voice_service, "asyncio", _FastAsyncio)
        connect_calls = {"n": 0}
        recovered = asyncio.Event()

        async def fake_connect():
            connect_calls["n"] += 1
            if connect_calls["n"] <= 3:
                raise RuntimeError("upstream down at session start")
            recovered.set()
            await asyncio.Event().wait()

        async def fake_recv():
            return

        async def fake_send():
            await asyncio.Event().wait()

        monkeypatch.setattr(asr, "_funasr_connect", fake_connect)
        monkeypatch.setattr(asr, "_funasr_recv", fake_recv)
        monkeypatch.setattr(asr, "_funasr_send", fake_send)
        asr._start_done = asyncio.Event()

        sup = asyncio.ensure_future(asr._funasr_supervisor())
        try:
            await asyncio.wait_for(recovered.wait(), timeout=10)
        finally:
            sup.cancel()
            try:
                await sup
            except asyncio.CancelledError:
                pass

        assert connect_calls["n"] >= 4, (
            f"first-connect failure stopped the supervisor after {connect_calls['n']} tries"
        )
        assert asr._start_done.is_set(), "session startup must not remain blocked"
        errors = []
        while not queue.empty():
            ev = queue.get_nowait()
            if ev.get("type") == "error":
                errors.append(ev)
        assert len(errors) == 1, f"exactly one outage notice expected, got {errors}"

    asyncio.run(run())


def test_supervisor_survives_six_failures_then_recovers(monkeypatch):
    async def run():
        queue = asyncio.Queue()
        asr = _VoiceASR(ASRService(), queue)

        monkeypatch.setattr(voice_service, "asyncio", _FastAsyncio)
        connect_calls = {"n": 0}
        ready_after_recovery = asyncio.Event()

        async def fake_connect():
            connect_calls["n"] += 1
            n = connect_calls["n"]
            if n == 1:
                return  # initial connect OK
            if n <= 7:
                raise RuntimeError(f"transient ASR outage #{n}")
            # Recovery reached — park here, the test is done.
            ready_after_recovery.set()
            await asyncio.Event().wait()

        async def fake_recv():
            # Upstream drops immediately after connect (or after recovery) —
            # drives the reconnect loop.
            return

        async def fake_send():
            await asyncio.Event().wait()

        monkeypatch.setattr(asr, "_funasr_connect", fake_connect)
        monkeypatch.setattr(asr, "_funasr_recv", fake_recv)
        monkeypatch.setattr(asr, "_funasr_send", fake_send)
        asr._start_done = asyncio.Event()

        sup = asyncio.ensure_future(asr._funasr_supervisor())
        try:
            await asyncio.wait_for(ready_after_recovery.wait(), timeout=10)
        finally:
            sup.cancel()
            try:
                await sup
            except asyncio.CancelledError:
                pass

        assert connect_calls["n"] >= 8, (
            f"supervisor gave up after {connect_calls['n']} connects "
            f"(>5 failures must keep retrying)"
        )
        errors = []
        while not queue.empty():
            ev = queue.get_nowait()
            if ev.get("type") == "error":
                errors.append(ev)
        assert len(errors) <= 1, f"at most one error notice expected, got {errors}"

    asyncio.run(run())
