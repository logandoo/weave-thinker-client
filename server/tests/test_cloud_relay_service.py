"""W32 Task 4 — 云端中继（方案 A 核心）：路由谓词 + 代理行为 + ASGI 泵。

契约（Consistency Hub）：
- `should_relay(user_id, target)`：云端链路在 且（target=None（云端）或
  device:<非本机且不在本地注册表>）→ True；本机/本地注册表/无链接/非法 → False。
- `proxy_to_cloud(user_id, method, path, query, body, headers)`：云端 Bearer 注入 +
  404 会话 ensure 重试一次；云端不可达 → LinkUnavailable（fail-closed 不落本地）。
- `relay_asgi(...)`：拦截面 /api/chat/stream* 与 /api/devices/{id}/fs/*；未命中或
  谓词 False → 原样放行下游 app；命中 → 云端响应（含 SSE）逐帧透传，502 明确报错。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_cloud_relay_service.py -v
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

from app.services import cloud_link_service as cls
from app.services import cloud_relay_service as relay


class FakeBinding:
    server_url = "https://cloud.example"
    cloud_username = "alice"
    cloud_password = "pw"


def _wire(monkeypatch, handler, *, linked=True, self_id="dev-self", local_ids=frozenset()):
    monkeypatch.setattr(cls, "_load_sync_binding", lambda user_id: FakeBinding() if linked else None)

    async def _async_binding(user_id):
        return FakeBinding() if linked else None

    monkeypatch.setattr(cls, "_load_sync_binding_async", _async_binding)
    monkeypatch.setattr(cls, "_wake_sync", lambda: None)
    monkeypatch.setattr(cls, "_poll_sleep", lambda s: asyncio.sleep(0))
    cls.cloud_link_service._tokens.clear()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="")
    monkeypatch.setattr(cls, "_get_client", lambda: client)
    async def _local_ids(user_id):
        return set(local_ids)

    monkeypatch.setattr(relay, "_local_device_ids", _local_ids)
    monkeypatch.setattr("app.services.device_pairing_service.load_binding",
                        lambda: {"device_id": self_id, "token": "t"})
    return client


# ---------------- RED-1：路由谓词四象限（Consistency Hub 行）----------------

def test_predicate_null_target_relays(monkeypatch):
    _wire(monkeypatch, lambda r: httpx.Response(200, json={}))
    assert asyncio.run(relay.should_relay("u1", None)) is True, "云端目标（null）必须中继"


def test_predicate_self_target_local(monkeypatch):
    _wire(monkeypatch, lambda r: httpx.Response(200, json={}), self_id="dev-self")
    assert asyncio.run(relay.should_relay("u1", "device:dev-self")) is False, "本机目标=本地执行不中继"


def test_predicate_other_device_relays(monkeypatch):
    _wire(monkeypatch, lambda r: httpx.Response(200, json={}), self_id="dev-self")
    assert asyncio.run(relay.should_relay("u1", "device:dev-other")) is True, "非本机设备目标必须中继"


def test_predicate_local_registry_device_local(monkeypatch):
    _wire(monkeypatch, lambda r: httpx.Response(200, json={}),
          self_id="dev-self", local_ids=frozenset({"dev-local"}))
    assert asyncio.run(relay.should_relay("u1", "device:dev-local")) is False, "本地注册表设备走本地派发"


def test_predicate_unlinked_local(monkeypatch):
    _wire(monkeypatch, lambda r: httpx.Response(200, json={}), linked=False)
    assert asyncio.run(relay.should_relay("u1", None)) is False, "无云端链接=本地执行（W26 退化）"
    assert asyncio.run(relay.should_relay("u1", "device:dev-other")) is False


def test_predicate_invalid_target_local(monkeypatch):
    _wire(monkeypatch, lambda r: httpx.Response(200, json={}))
    assert asyncio.run(relay.should_relay("u1", "bogus")) is False


# ---------------- RED-2：代理行为 ----------------

def test_proxy_replays_sse_frames_verbatim(monkeypatch):
    sse = b'data: {"content": "hi"}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.headers.get("authorization") == "Bearer tok"
        return httpx.Response(200, content=sse,
                              headers={"content-type": "text/event-stream"})

    _wire(monkeypatch, handler)
    resp = asyncio.run(relay.proxy_to_cloud(
        "u1", "POST", "/api/chat/stream", "", b'{"conversation_id": "c1"}', {}))
    assert resp.status_code == 200
    assert resp.read() == sse, "SSE 帧必须逐字节透传"
    assert resp.headers["content-type"].startswith("text/event-stream")


def test_proxy_retries_once_after_ensure_conversation(monkeypatch):
    state = {"gets": 0, "stream": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path == "/api/conversations/c-new":
            state["gets"] += 1
            return httpx.Response(200 if state["gets"] > 1 else 404, json={"id": "c-new"})
        state["stream"] += 1
        return httpx.Response(200 if state["stream"] > 1 else 404,
                              content=b"data: [DONE]\n\n" if state["stream"] > 1 else b'{"detail": "no conv"}',
                              headers={"content-type": "text/event-stream" if state["stream"] > 1 else "application/json"})

    _wire(monkeypatch, handler)
    resp = asyncio.run(relay.proxy_to_cloud(
        "u1", "POST", "/api/chat/stream", "", b'{"conversation_id": "c-new"}', {}))
    assert resp.status_code == 200, "ensure 后重试一次应成功"
    assert state["stream"] >= 2 and state["gets"] >= 2


def test_proxy_cloud_down_raises_unavailable(monkeypatch):
    def _down(request):
        raise httpx.ConnectError("boom")

    _wire(monkeypatch, _down)
    from app.services.cloud_link_service import LinkUnavailable
    with pytest.raises(LinkUnavailable):
        asyncio.run(relay.proxy_to_cloud(
            "u1", "POST", "/api/chat/stream", "", b'{"conversation_id": "c1"}', {}))


# ---------------- RED-3：ASGI 泵（拦截/放行/502）----------------

def _asgi_call(path, body=b"", method="POST"):
    sent = []
    scope = {
        "type": "http", "method": method, "path": path, "query_string": b"",
        "headers": [(b"authorization", b"Bearer local-tok")],
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(msg):
        # 严格 ASGI 契约校验（评审 B Critical：http.body 是 receive 类型，
        # 发出即 uvicorn RuntimeError——这里对任何错误类型直接断言失败）
        assert msg["type"] in ("http.response.start", "http.response.body"), msg["type"]
        sent.append(msg)

    return scope, receive, send, sent


def test_asgi_passes_through_when_not_intercepted(monkeypatch):
    _wire(monkeypatch, lambda r: httpx.Response(200, json={}))
    called = []

    async def downstream(scope, receive, send):
        called.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"local"})

    scope, receive, send, sent = _asgi_call("/api/conversations/c1", b"{}", method="GET")
    async def _resolve(*a, **k):
        return ("u1", "device:dev-other", None)

    monkeypatch.setattr(relay, "_resolve_user_and_target", _resolve)
    asyncio.run(relay.relay_asgi(scope, receive, send, downstream))
    assert called == ["/api/conversations/c1"], "非拦截面必须放行下游"


def test_asgi_relays_chat_stream_and_skips_downstream(monkeypatch):
    sse = b'data: {"ok": 1}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, content=sse,
                              headers={"content-type": "text/event-stream"})

    _wire(monkeypatch, handler)
    called = []

    async def downstream(scope, receive, send):
        called.append(scope["path"])

    scope, receive, send, sent = _asgi_call("/api/chat/stream", b'{"conversation_id": "c1"}')
    async def _resolve(*a, **k):
        return ("u1", None, "c1")

    monkeypatch.setattr(relay, "_resolve_user_and_target", _resolve)
    asyncio.run(relay.relay_asgi(scope, receive, send, downstream))
    assert called == [], "中继命中不得触达下游 handler"
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert body == sse, "SSE 帧必须原样透传给前端"


def test_asgi_replay_keeps_original_receive_alive(monkeypatch):
    """评审 B Critical 回归：重放 body 后不得合成 http.disconnect——下游的
    断连监听（sse_starlette 同款）把它当客户端离开会砍掉本地流。"""
    _wire(monkeypatch, lambda r: httpx.Response(200, json={}))
    seen = []

    async def downstream(scope, receive, send):
        msg1 = await receive()
        seen.append(msg1["type"])
        assert msg1["type"] == "http.request" and msg1["body"] == b'{"x": 1}'
        # 模拟 sse_starlette 的 _listen_for_disconnect：并行等断连——必须阻塞
        # 在原 receive 上，而不是立刻拿到合成的 http.disconnect
        msg2 = await asyncio.wait_for(receive(), timeout=0.05)
        seen.append(msg2["type"])

    scope, _unused, send, sent = _asgi_call("/api/chat/stream", b'{"x": 1}')
    body_msgs = [{"type": "http.request", "body": b'{"x": 1}', "more_body": False}]

    async def receive():
        # 真实通道：先给请求体（中间件缓冲读取），之后客户端未断开=阻塞
        if body_msgs:
            return body_msgs.pop(0)
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def resolve(*a, **k):
        return ("u1", "device:dev-self", None)

    monkeypatch.setattr(relay, "_resolve_user_and_target", resolve)
    async def run():
        return await asyncio.wait_for(
            relay.relay_asgi(scope, receive, send, downstream), timeout=1.0)

    try:
        asyncio.run(run())
    except asyncio.TimeoutError:
        pass  # 预期：downstream 第二次 receive 阻塞在原通道（1s 超时=未合成 disconnect）
    assert seen == ["http.request"], f"重放后不得合成 disconnect，实际={seen}"


def test_asgi_cloud_down_returns_502(monkeypatch):
    def _down(request):
        raise httpx.ConnectError("boom")

    _wire(monkeypatch, _down)

    async def downstream(scope, receive, send):
        raise AssertionError("中继失败不得落下游（D-8 不静默改道）")

    scope, receive, send, sent = _asgi_call("/api/chat/stream", b'{"conversation_id": "c1"}')
    async def _resolve(*a, **k):
        return ("u1", None, "c1")

    monkeypatch.setattr(relay, "_resolve_user_and_target", _resolve)
    asyncio.run(relay.relay_asgi(scope, receive, send, downstream))
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 502
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert "云端" in body.decode("utf-8"), f"502 detail 须明确，实际={body!r}"


# ---------------- 判据 #99：设备文件代理面（fs 回退云端腿）----------------

def test_predicate_fs_relays_to_cloud_for_unknown_device(monkeypatch):
    """非本机、不在本地注册表的设备 fs 请求 → 代理云端设备代理（非 404）。"""
    _wire(monkeypatch, lambda r: httpx.Response(200, json={}), self_id="dev-self")
    assert asyncio.run(relay.should_relay_fs("u1", "dev-other")) is True


def test_predicate_fs_local_for_self_and_registry(monkeypatch):
    _wire(monkeypatch, lambda r: httpx.Response(200, json={}),
          self_id="dev-self", local_ids=frozenset({"dev-local"}))
    assert asyncio.run(relay.should_relay_fs("u1", "dev-self")) is False, "本机文件=本地 workspace"
    assert asyncio.run(relay.should_relay_fs("u1", "dev-local")) is False, "本地注册表设备走本地代理"


def test_asgi_fs_relay_skips_downstream(monkeypatch):
    """fs 拦截面：命中中继 → 云端设备代理响应透传，下游本地 fs 代理不被触达。"""
    payload = b'PK\x03\x04fake-zip'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.url.path == "/api/devices/dev-other/fs/zip"
        return httpx.Response(200, content=payload,
                              headers={"content-type": "application/zip"})

    _wire(monkeypatch, handler, self_id="dev-self")
    called = []

    async def downstream(scope, receive, send):
        called.append(scope["path"])

    scope, receive, send, sent = _asgi_call("/api/devices/dev-other/fs/zip", b"", method="GET")
    monkeypatch.setattr(relay, "_resolve_user_and_target", _ := (lambda *a, **k: None))
    async def resolve(*a, **k):
        return ("u1", "device:dev-other", None)
    monkeypatch.setattr(relay, "_resolve_user_and_target", resolve)
    asyncio.run(relay.relay_asgi(scope, receive, send, downstream))
    assert called == [], "fs 中继命中不得触达本地代理"
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert body == payload, "设备字节必须原样透传"


def test_resolve_user_and_target_decodes_real_token():
    """W37 regression: relay must import decode_access_token from auth_service.

    Previously `from app.core.security import decode_access_token` raised
    ModuleNotFoundError on every device-target request; the middleware fell back
    to local handling / hung. A no-conversation token decode is enough to pin the
    import without a DB fixture.
    """
    from app.services.auth_service import create_access_token

    token = create_access_token("u-relay-test", "relay")
    scope = {
        "type": "http",
        "path": "/api/chat/stream",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    user_id, target, conv_id = asyncio.run(
        relay._resolve_user_and_target(scope, b"{}", "chat"))
    assert user_id == "u-relay-test"
    assert target is None and conv_id is None
