"""听写 ASR 通路保活/断开回归 — run-task 参数面与语音助理对齐 + 上游掉线异常关闭。

听写面 = `/api/asr/ws/transcribe/stream` → `ASRService.proxy_funasr_websocket_stream`
（ChatInput / NoteEditor / QuickNoteButton / ZenNotePanel 共用）。

判据 #11/#12：run-task `parameters` 必须带 `heartbeat` / `max_sentence_silence` /
`multi_threshold_mode_enabled` / `speech_noise_threshold`。官方文档（百炼
Fun-ASR-Realtime 客户端事件）明载 `heartbeat` 缺省 false 时「即使持续发送静音音频，
连接也将在 60 秒后因超时而断开」——听写面曾四键全缺（上游 A4.9 deferred Minor）。

判据 #13：上游 ASR WS 中途掉线不得硬停听写会话——服务端须以异常关闭码（非 1000/1005）
告知客户端，交由 `useAsrStreaming.handleSocketClose` 的 scheduleReconnect 收编。

Run from server/:  ../.localstack/venv/bin/python -m pytest tests/test_asr_dictation_keepalive.py -v
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import app.services.asr_service as asr_mod
from app.services.asr_service import (
    ASRService,
    WS_CLOSE_FATAL,
    WS_CLOSE_NORMAL,
    WS_CLOSE_UPSTREAM_DROP,
    _is_fatal_connect_error,
)
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, InvalidHandshake, InvalidStatusCode
from websockets.frames import Close


def _drop(reason: str = "upstream drop") -> ConnectionClosed:
    """可抛出的上游掉线异常（relay_from_* 只认 ConnectionClosed）。"""
    return ConnectionClosedError(Close(1011, reason), None)


class _AsrStub(ASRService):
    """注入 provider/model/调参配置的最小桩（不走 __init__/配置单例）。"""

    def __init__(self, model: str = "fun-asr-realtime", asr_cfg: dict | None = None):
        self._model = model
        self._cfg = asr_cfg if asr_cfg is not None else {
            "max_sentence_silence": 800,
            "ws_open_timeout": 5,
            "ws_ping_interval": 20,
            "ws_ping_timeout": 20,
            "ws_close_timeout": 5,
        }

    @property
    def _asr_config(self) -> dict:
        return self._cfg

    @property
    def dashscope_model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "dashscope"

    @property
    def dashscope_api_key(self) -> str:
        return "test-key"

    @property
    def hotword_phonetic_correction(self) -> bool:
        return False


class _FakeUpstream:
    """伪 DashScope WS：记录 send()，按脚本回放 recv()。"""

    def __init__(self, recv_script: list):
        self.sent: list = []
        self._recv_script = list(recv_script)
        self.closed = False

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        if self._recv_script:
            item = self._recv_script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        raise _drop("script exhausted")

    async def close(self):
        self.closed = True


class _FakeClient:
    """伪客户端 WS：回放 receive()，记录 send_json/close。"""

    def __init__(self, receive_script: list):
        self._receive_script = list(receive_script)
        self.sent_json: list = []
        self.close_calls: list = []

    async def receive(self):
        if self._receive_script:
            item = self._receive_script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        await asyncio.Event().wait()

    async def send_json(self, payload):
        self.sent_json.append(payload)

    async def close(self, code: int = 1000, reason: str = ""):
        self.close_calls.append({"code": code, "reason": reason})


def _start_frame(**extra) -> dict:
    return {"type": "websocket.receive", "text": json.dumps({"event": "start", "language": "zh", **extra})}


def _run_task_from(upstream: _FakeUpstream) -> dict:
    for item in upstream.sent:
        if isinstance(item, str):
            msg = json.loads(item)
            if msg.get("header", {}).get("action") == "run-task":
                return msg
    raise AssertionError(f"no run-task frame captured: {upstream.sent!r}")


def _drive(monkeypatch, svc: ASRService, upstream: _FakeUpstream, client: _FakeClient):
    async def _fake_connect(*args, **kwargs):
        return upstream

    monkeypatch.setattr(asr_mod.websockets, "connect", _fake_connect)

    async def _run():
        return await asyncio.wait_for(
            svc.proxy_funasr_websocket_stream(client, default_hotwords=[]), timeout=15
        )

    return asyncio.run(_run())


# ── 判据 #11：heartbeat 必须下发（缺省 false = 60s 静默超时断开，官方文档）──


def test_dictation_run_task_enables_heartbeat(monkeypatch):
    upstream = _FakeUpstream([
        json.dumps({"header": {"event": "task-started"}, "payload": {}}),
    ])
    client = _FakeClient([_start_frame()])
    _drive(monkeypatch, _AsrStub(), upstream, client)

    params = _run_task_from(upstream)["payload"]["parameters"]
    assert params.get("heartbeat") is True, (
        "听写面 run-task 必须 heartbeat=true；缺省 false 时官方文档明载"
        "「即使持续发送静音音频，连接也将在 60 秒后因超时而断开」→ 用户侧表现为"
        "「语音识别很容易断开」"
    )


# ── 判据 #12：断句/防切/VAD 噪声阈值与语音助理通路同面 ──


def test_dictation_run_task_matches_voice_param_surface(monkeypatch):
    upstream = _FakeUpstream([
        json.dumps({"header": {"event": "task-started"}, "payload": {}}),
    ])
    client = _FakeClient([_start_frame()])
    _drive(monkeypatch, _AsrStub(asr_cfg={
        "max_sentence_silence": 800,
        "ws_open_timeout": 5, "ws_ping_interval": 20,
        "ws_ping_timeout": 20, "ws_close_timeout": 5,
    }), upstream, client)

    params = _run_task_from(upstream)["payload"]["parameters"]
    assert params.get("max_sentence_silence") == 800, (
        "max_sentence_silence 须取 [asr] 配置（语音助理面 voice_service._funasr_connect 同源）；"
        "缺省则落服务端 1300ms，断句手感与语音助理不一致"
    )
    assert params.get("multi_threshold_mode_enabled") is True, (
        "multi_threshold_mode_enabled 须与语音助理面一致（防 VAD 切断长句）"
    )
    assert "speech_noise_threshold" in params, (
        "speech_noise_threshold 须与语音助理面同旋钮（voice_asr_speech_noise_threshold）"
    )


def test_dictation_speech_noise_threshold_reads_the_single_knob(monkeypatch):
    """值必须来自单旋钮（而非字面量）——改旋钮即两路同变，是「同源」的实证。"""
    cfg = asr_mod.get_config()
    monkeypatch.setattr(type(cfg), "voice_asr_speech_noise_threshold",
                        property(lambda self: 0.42))
    upstream = _FakeUpstream([
        json.dumps({"header": {"event": "task-started"}, "payload": {}}),
    ])
    client = _FakeClient([_start_frame()])
    _drive(monkeypatch, _AsrStub(), upstream, client)
    got = _run_task_from(upstream)["payload"]["parameters"].get("speech_noise_threshold")
    assert got == pytest.approx(0.42), f"须取 voice_asr_speech_noise_threshold 单旋钮；实得 {got}"


def test_dictation_max_sentence_silence_reads_asr_config(monkeypatch):
    upstream = _FakeUpstream([
        json.dumps({"header": {"event": "task-started"}, "payload": {}}),
    ])
    client = _FakeClient([_start_frame()])
    _drive(monkeypatch, _AsrStub(asr_cfg={
        "max_sentence_silence": 1200,
        "ws_open_timeout": 5, "ws_ping_interval": 20,
        "ws_ping_timeout": 20, "ws_close_timeout": 5,
    }), upstream, client)

    assert _run_task_from(upstream)["payload"]["parameters"]["max_sentence_silence"] == 1200


# ── 判据 #13：上游掉线 → 异常关闭码（客户端重连机制据此接棒）──


def test_upstream_drop_closes_client_with_abnormal_code(monkeypatch):
    """上游 ConnectionClosed 后不得只发 error 就干净 return（1000=客户端主动，重连被绕过）。"""
    closed = _drop("upstream drop")
    upstream = _FakeUpstream([
        json.dumps({"header": {"event": "task-started"}, "payload": {}}),
        closed,
    ])
    client = _FakeClient([_start_frame()])
    _drive(monkeypatch, _AsrStub(), upstream, client)

    assert any(m.get("event") == "error" for m in client.sent_json), "须告知客户端断开原因"
    assert client.close_calls, "上游掉线后必须显式 close 客户端 WS，否则 FastAPI 以 1000 干净收尾"
    codes = [c["code"] for c in client.close_calls]
    # 必须**恰好** WS_CLOSE_UPSTREAM_DROP(1011)：客户端 useAsrStreaming 把 1008 也判为
    # 「停止」，若此处退化成 1008 则每一次瞬断都硬停——正是判据 #13 要消灭的缺陷，
    # 却会带着绿测出货（A4.9 R2-B I#1）。
    assert codes == [WS_CLOSE_UPSTREAM_DROP], (
        f"上游掉线须以且仅以 WS_CLOSE_UPSTREAM_DROP=1011 收尾；实得 {codes}"
    )
    drop_frames = [m for m in client.sent_json if m.get("event") == "error"]
    assert drop_frames and drop_frames[0].get("fatal") is False, (
        "掉线 error 帧须标 fatal=False（客户端据此走重连而非立即拆会话）"
    )


# ── 防漂移锚：保活/断句参数单一事实源，双通路必须同源 ──


def test_keepalive_helper_emits_full_surface():
    from app.services.asr_service import _keepalive_run_task_params

    got = _keepalive_run_task_params({"max_sentence_silence": 900}, 0.4)
    assert got == {
        "max_sentence_silence": 900,
        "multi_threshold_mode_enabled": True,
        "heartbeat": True,
        "speech_noise_threshold": 0.4,
    }
    assert _keepalive_run_task_params({}, 0.3)["max_sentence_silence"] == 800, "缺省 800（非服务端 1300）"


def test_both_paths_wired_to_shared_keepalive_helper():
    """语音面/听写面 run-task 构造必须同源——本次缺陷的结构性根因即两面参数漂移。"""
    import inspect

    from app.services import asr_service as asr_m
    from app.services import voice_service as voice_m

    voice_src = inspect.getsource(voice_m)
    proxy_src = inspect.getsource(asr_m.ASRService.proxy_funasr_websocket_stream)
    funasr_src = inspect.getsource(voice_m._VoiceASR._funasr_connect) if hasattr(voice_m, "_VoiceASR") else ""
    if not funasr_src:
        for name in dir(voice_m):
            obj = getattr(voice_m, name)
            if inspect.isclass(obj) and hasattr(obj, "_funasr_connect"):
                funasr_src = inspect.getsource(obj._funasr_connect)
                break
    assert "_keepalive_run_task_params" in proxy_src, "听写面须接线 _keepalive_run_task_params"
    assert "params.update(_keepalive_run_task_params" in funasr_src, (
        "_funasr_connect 须以 params.update 接线共享 helper（仅 import 不算接线）"
    )
    assert "_keepalive_run_task_params" in funasr_src, "语音面 run-task 构造（_funasr_connect）须接线共享 helper"
    assert '"heartbeat"' not in funasr_src, (
        "_funasr_connect 不得再内联 heartbeat 字面量（须走共享 helper，否则两面再次漂移）"
    )


# ── A4.9 R2-C1/I1：致命错误不得触发重连风暴 ──


def test_task_failed_is_fatal_and_stops(monkeypatch):
    """task-failed（key 失效/配额/参数不支持）须 1008 + fatal=True，客户端立刻拆会话。

    若误标成瞬断，客户端会 scheduleReconnect → 每轮重新 run-task → 10 轮风暴
    （MAX_RECONNECT_ATTEMPTS=10），正是 A4.9 R2-C1 要消灭的形态。"""
    upstream = _FakeUpstream([
        json.dumps({"header": {"event": "task-started"}, "payload": {}}),
        json.dumps({"header": {"event": "task-failed", "error_message": "quota exceeded"}, "payload": {}}),
    ])
    client = _FakeClient([_start_frame()])

    # 用例夹具修复（2026-10-10，断言不变）：此前的 _MP 假 monkeypatch 是裸
    # setattr 无 undo，把 _fake_connect 泄漏到共享 websockets 模块——后续任何
    # `async with websockets.connect` 的测试被毒化（device executor 回路实证）。
    _drive(monkeypatch, _AsrStub(), upstream, client)

    codes = [c["code"] for c in client.close_calls]
    assert codes == [WS_CLOSE_FATAL], f"task-failed 须以 WS_CLOSE_FATAL=1008 收尾；实得 {codes}"
    frames = [m for m in client.sent_json if m.get("event") == "error"]
    assert frames and frames[-1].get("fatal") is True, "task-failed 的 error 帧须标 fatal=True"


def test_connect_auth_failure_is_fatal():
    """建连被拒（401/403/握手失败）重试必复现 ⇒ 判致命，不得走 1011 瞬断重连。"""
    assert _is_fatal_connect_error(InvalidHandshake(401, "Unauthorized", {})) is True
    assert _is_fatal_connect_error(InvalidStatusCode(403, "Forbidden")) is True
    assert _is_fatal_connect_error(ConnectionClosedError(Close(1011, "net"), None)) is False
    assert _is_fatal_connect_error(OSError("connection reset by peer")) is False


def test_connect_failure_closes_with_classified_code(monkeypatch):
    async def _boom(*a, **k):
        raise InvalidStatusCode(401, "Unauthorized")

    monkeypatch.setattr(asr_mod.websockets, "connect", _boom)
    client = _FakeClient([_start_frame()])
    asyncio.run(_AsrStub().proxy_funasr_websocket_stream(client, default_hotwords=[]))
    codes = [c["code"] for c in client.close_calls]
    assert codes == [WS_CLOSE_FATAL], f"鉴权被拒须 1008；实得 {codes}"
    frames = [m for m in client.sent_json if m.get("event") == "error"]
    assert frames and frames[-1].get("fatal") is True
