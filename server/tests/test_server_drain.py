"""RED tests (A4.8) — 部署排空（2026-10-06，conv ae9aa092 P2）。

契约：
  R1 registry：set_draining/is_draining/active_run_count（provisional 预约槽计入）
  R2 端点仅 loopback：非回环 403
  R3 chat 入口 draining 时 503 且零写副作用（检查在 chat_stream 任何写库之前）
  R4 stop.sh 携带排空逻辑（drain + WT_DRAIN_TIMEOUT 轮询）

Run: cd backend && python -m pytest tests/test_server_drain.py -v
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run(coro):
    return asyncio.run(coro)


# ── R1：registry ─────────────────────────────────────────────────────────

def test_registry_drain_flag_and_count():
    from app.services.active_agent_registry import ActiveAgentRegistry
    reg = ActiveAgentRegistry.__new__(ActiveAgentRegistry)
    import asyncio as _aio
    reg._agents = {}
    reg._lock = _aio.Lock()
    reg._draining = False
    reg._drain_watchdog = None
    reg._drain_ttl_seconds = 600.0
    assert reg.is_draining is False
    _run(reg.set_draining(True))
    assert reg.is_draining is True

    from app.services.active_agent_registry import ActiveAgentState
    reg._agents["c1"] = ActiveAgentState(conversation_id="c1", is_running=True)
    reg._agents["c2"] = ActiveAgentState(conversation_id="c2", is_running=True, provisional=True)
    reg._agents["c3"] = ActiveAgentState(conversation_id="c3", is_running=False)
    assert _run(reg.active_run_count()) == 2, "provisional 预约槽必须计入（即将启动的 run）"


# ── R2：loopback + 令牌双闸（A4.9 r1 I5）─────────────────────────────────

def test_loopback_guard():
    from app.api.server_admin import _loopback_only
    from fastapi import HTTPException
    ok = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    _loopback_only(ok)  # 不抛
    v6 = SimpleNamespace(client=SimpleNamespace(host="::1"))
    _loopback_only(v6)
    bad = SimpleNamespace(client=SimpleNamespace(host="10.0.0.5"))
    with pytest.raises(HTTPException) as ei:
        _loopback_only(bad)
    assert ei.value.status_code == 403
    none_client = SimpleNamespace(client=None)
    with pytest.raises(HTTPException):
        _loopback_only(none_client)


def test_drain_token_gate():
    """令牌闸：配置令牌后，无令牌/错令牌 403，对令牌放行——本机恶意网页
    （CORS * + loopback）不再能打进 drain。"""
    import app.api.server_admin as sa
    from fastapi import HTTPException
    sa.set_drain_token("tok123")
    try:
        no_tok = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
        with pytest.raises(HTTPException) as ei:
            sa._check_auth(no_tok)
        assert ei.value.status_code == 403
        bad_tok = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                                  headers={"X-Drain-Token": "wrong"})
        with pytest.raises(HTTPException):
            sa._check_auth(bad_tok)
        ok = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                             headers={"X-Drain-Token": "tok123"})
        sa._check_auth(ok)  # 不抛
        # 令牌再对、非 loopback 也拒（纵深）
        remote = SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"),
                                 headers={"X-Drain-Token": "tok123"})
        with pytest.raises(HTTPException):
            sa._check_auth(remote)
    finally:
        sa.set_drain_token("")

    # scoped 复审 Minor：令牌未配置 = fail-closed 503（不退化为仅 loopback）
    from fastapi import HTTPException as _HE
    no_tok = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    try:
        sa._check_auth(no_tok)
        assert False, "令牌未配置必须 503"
    except _HE as e:
        assert e.status_code == 503


def test_drain_ttl_watchdog():
    """A4.9 r1 I6：drain TTL 自动解除——stop.sh 中止不得永久 503。"""
    from app.services.active_agent_registry import ActiveAgentRegistry
    import asyncio as _aio

    async def main():
        reg = ActiveAgentRegistry.__new__(ActiveAgentRegistry)
        reg._agents = {}
        reg._lock = _aio.Lock()
        reg._draining = False
        reg._drain_ttl_seconds = 0.05
        reg._drain_watchdog = None
        await reg.set_draining(True)
        assert reg.is_draining is True
        await _aio.sleep(0.15)
        assert reg.is_draining is False, "TTL 到期必须自动解除 drain"
        # off 取消看门狗
        await reg.set_draining(True)
        await reg.set_draining(False)
        await _aio.sleep(0.15)
        assert reg.is_draining is False

    _run(main())


# ── R3：chat 入口 503 且零写副作用 ───────────────────────────────────────

def test_chat_drain_gate_before_any_write():
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "app", "api",
                     "chat_stream_transport.py"),
        encoding="utf-8").read()
    fn_start = src.index("async def stream_chat_response(")
    gate = src.index("is_draining", fn_start)
    first_write = src.index("db.add(", fn_start)
    assert gate < first_write, "drain 503 必须在任何写库之前（不留半拉子持久化状态）"
    assert "Retry-After" in src[gate:gate + 400]


# ── R4：stop.sh 排空 ─────────────────────────────────────────────────────

def test_stop_script_drains():
    # vendor 布局适配（2026-10-06 W4）：上游 scripts/stop.sh → script/linux/localstack.sh
    # backend_stop 路径（test-change 已登记；契约断言原样：drain→轮询→kill 顺序）
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "script", "linux", "localstack.sh"),
        encoding="utf-8").read()
    assert "/api/server/drain" in src
    assert "/api/server/active-runs" in src
    assert "WT_DRAIN_TIMEOUT" in src
    stop_fn = src.index("backend_stop()")
    drain_pos = src.index("_drain_wait", stop_fn)
    kill_pos = src.index('kill "$(cat "$BPID")"', stop_fn)
    assert drain_pos < kill_pos, "排空必须在 kill 之前"


# ── R5：排空扩面（D-5/D-9 revisit-if 落地，D-12）─────────────────────────

def test_resume_stream_drain_gate():
    """resume_stream 必须在 drain 中 503+Retry-After——排空窗口拒收新客户端流
    接入，先于 buffer 访问。"""
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "app", "api", "chat.py"),
        encoding="utf-8").read()
    fn_start = src.index("async def resume_stream(")
    fn_end = src.index("async def resume_generator(", fn_start)
    body = src[fn_start:fn_end]
    assert "is_draining" in body, "resume_stream 缺 drain 门"
    gate = body.index("is_draining")
    buf = body.index("stream_buffer_manager.get_buffer")
    assert gate < buf, "drain 门必须先于 buffer 访问"
    assert "Retry-After" in body[gate:gate + 400]


def test_voice_ws_drain_gate():
    """语音新会话：drain 中 accept 后 WS 1013 TRY_AGAIN_LATER 关闭（1013 必须
    真实送达客户端=accept 后 close；且门先于会话建立）。"""
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "app", "api", "voice.py"),
        encoding="utf-8").read()
    fn_start = src.index("async def voice_duplex_ws(")
    fn_end = src.index("await session.run()", fn_start)
    body = src[fn_start:fn_end]
    assert "is_draining" in body, "voice WS 缺 drain 门"
    assert "WS_1013_TRY_AGAIN_LATER" in body, "拒接码必须 1013 TRY_AGAIN_LATER"
    assert body.index("is_draining") < body.index("VoiceDuplexSession("), \
        "门必须先于会话建立"
    accept_at = body.index("await websocket.accept()")
    close13_at = body.index("WS_1013_TRY_AGAIN_LATER", accept_at)
    assert close13_at > accept_at, "1013 close 必须在 accept 之后（否则到不了客户端）"


def test_voice_session_counted_and_released():
    """语音在途会话计入排空计数：run 生命周期 begin/end 配对（异常也释放）。"""
    import app.services.voice_service as vs
    assert hasattr(vs, "active_session_count"), "缺 active_session_count"
    assert hasattr(vs, "begin_session") and hasattr(vs, "end_session")
    base = vs.active_session_count()
    vs.begin_session()
    assert vs.active_session_count() == base + 1
    vs.end_session()
    assert vs.active_session_count() == base, "end 必须配对释放"


def test_worker_stops_claiming_when_draining():
    """部署 drain 中后台轮询停止领取新任务（在途照常完成；未领留给下个进程）。"""
    from app.services.agent_worker import AgentWorker
    from app.services.active_agent_registry import ActiveAgentRegistry

    fake_reg = type("R", (), {"is_draining": True})()
    orig = ActiveAgentRegistry.get_instance
    ActiveAgentRegistry.get_instance = classmethod(lambda cls: fake_reg)

    class _Bomb:
        def __call__(self, *a, **k):
            raise AssertionError("drain 中不得开库领取任务")

    import app.services.agent_worker as aw_mod
    orig_db = aw_mod.AsyncSessionLocal
    aw_mod.AsyncSessionLocal = _Bomb()
    try:
        w = AgentWorker.__new__(AgentWorker)
        w._running_task_ids = set()
        w._worker_tasks = set()
        w._draining = False
        _run(w._poll_pending_tasks())  # 不得触库
    finally:
        aw_mod.AsyncSessionLocal = orig_db
        ActiveAgentRegistry.get_instance = orig


def test_active_runs_includes_voice_and_bg():
    """排空计数三源求和：registry（含预约槽）+ 后台 running + 语音在途。"""
    from app.api import server_admin as sa

    fake_reg = type("R", (), {
        "is_draining": False,
        "active_run_count": staticmethod(lambda: _fake_await(2)),
    })()
    orig_reg = sa.ActiveAgentRegistry.get_instance
    sa.ActiveAgentRegistry.get_instance = classmethod(lambda cls: fake_reg)
    import app.services.voice_service as vs
    orig_vs, orig_aw = vs.active_session_count, None
    vs.active_session_count = lambda: 3
    import app.services.agent_worker as aw
    orig_aw = aw.agent_worker.running_count
    aw.agent_worker.running_count = lambda: 4
    try:
        assert _run(sa._active_run_total()) == 9, "active 必须含语音(3)+后台(4)+registry(2)"
    finally:
        sa.ActiveAgentRegistry.get_instance = orig_reg
        vs.active_session_count = orig_vs
        aw.agent_worker.running_count = orig_aw


def _fake_await(v):
    async def _c():
        return v
    return _c()
