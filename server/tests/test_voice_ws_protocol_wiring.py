"""W39：voice WS 的 `audio_protocol` 查询参数**接线**契约（上游 handoff T9 的本地对应）。

上游 `test_voice_audio_protocol.py` 用 `_DummyWS` 直接赋 `session._audio_protocol`，
从未穿越 `server/app/api/voice.py` 的 `websocket.query_params.get("audio_protocol")`
⇒ 参数一旦改名/被中间层丢弃，单测仍全绿而线上静默退回 legacy（二进制 PCM）。

本测走**真实 ASGI 路由**（`TestClient.websocket_connect`），只替换三处边界：
  - `get_config` → 桩（`voice_enabled=True`，不依赖实例配置）
  - `get_current_user_from_websocket` → 桩用户（鉴权面由其它测试覆盖）
  - `VoiceDuplexSession` → 间谍类（记录构造实参后立即返回，不跑 ASR/TTS）

断言：`?audio_protocol=epoch_v1` → 会话收到 `"epoch_v1"`；不带该参数 → `None`
（服务端自行归一为 `legacy`，见 `voice_service.VoiceDuplexSession.__init__`）。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_voice_ws_protocol_wiring.py -v
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


def _install_spies(monkeypatch):
    """把三处边界替换成桩；返回捕获字典（每次调用后重置）。"""
    import app.api.voice as voice_api
    from main import app

    captured: dict = {"sessions": []}

    class SpySession:
        def __init__(self, websocket, user, db, conversation_id=None, audio_protocol=None):
            captured["sessions"].append(
                {"conversation_id": conversation_id, "audio_protocol": audio_protocol}
            )

        async def run(self) -> None:
            return

    async def _fake_ws_user(websocket, db):
        return SimpleNamespace(id="w39-user", username="w39")

    monkeypatch.setattr(voice_api, "get_config", lambda: SimpleNamespace(voice_enabled=True))
    monkeypatch.setattr(voice_api, "get_current_user_from_websocket", _fake_ws_user)
    monkeypatch.setattr(voice_api, "VoiceDuplexSession", SpySession)

    from fastapi.testclient import TestClient

    return TestClient(app), captured


def test_audio_protocol_epoch_v1_reaches_session(monkeypatch):
    """带 `audio_protocol=epoch_v1` → 会话构造实参必须是 `epoch_v1`（不是 legacy）。"""
    client, captured = _install_spies(monkeypatch)
    with client.websocket_connect("/api/voice/ws?audio_protocol=epoch_v1&conversation_id=conv-w39"):
        pass

    assert captured["sessions"], "VoiceDuplexSession was never constructed — route wiring broken"
    kwargs = captured["sessions"][0]
    assert kwargs["audio_protocol"] == "epoch_v1", (
        f"query param did not reach the session: audio_protocol={kwargs['audio_protocol']!r}"
    )
    assert kwargs["conversation_id"] == "conv-w39", "conversation_id passthrough regressed"


def test_audio_protocol_absent_stays_legacy(monkeypatch):
    """不带 `audio_protocol` → 会话收到 None（服务端归一为 legacy，旧客户端不静音）。"""
    client, captured = _install_spies(monkeypatch)
    with client.websocket_connect("/api/voice/ws"):
        pass

    assert captured["sessions"], "VoiceDuplexSession was never constructed"
    assert captured["sessions"][0]["audio_protocol"] is None, (
        "absent audio_protocol must stay None so the server falls back to legacy binary PCM"
    )


def test_unknown_audio_protocol_is_passed_through_and_normalized_by_session(monkeypatch):
    """未知值原样透传（归一由 session 负责：只有 epoch_v1 才启用新协议）。"""
    client, captured = _install_spies(monkeypatch)
    with client.websocket_connect("/api/voice/ws?audio_protocol=epoch_v2"):
        pass

    assert captured["sessions"][0]["audio_protocol"] == "epoch_v2"
    # 归一语义（epoch_v1 → 新协议，其它/缺省 → legacy）由会话自身承担，行为面已由
    # 上游 `test_voice_audio_protocol.py::test_audio_protocol_negotiation_preserves_legacy_clients`
    # 覆盖；此处只钉「路由层原样透传」，不再做源码文本断言（脆且重复）。


def test_missing_token_closes_policy_violation(monkeypatch):
    """鉴权面：无 token → 握手被拒（1008/403），不会进入会话构造。"""
    import app.api.voice as voice_api
    from main import app

    captured = {"constructed": False}

    class SpySession:
        def __init__(self, *a, **kw):
            captured["constructed"] = True

    monkeypatch.setattr(voice_api, "get_config", lambda: SimpleNamespace(voice_enabled=True))
    monkeypatch.setattr(voice_api, "VoiceDuplexSession", SpySession)

    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(app)
    # 精确断言：必须是 1008（policy violation），不是任意异常（DB 错误/1000/1013 都不算通过）
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/api/voice/ws"):
            pass
    assert excinfo.value.code == 1008, (
        f"unauthenticated WS must close with 1008 policy violation, got {excinfo.value.code}"
    )
    assert captured["constructed"] is False, "unauthenticated WS must not construct a session"
