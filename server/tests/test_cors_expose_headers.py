"""UPSTREAM_TODO_20261007 项2（06 档项13）：CORSMiddleware 必须 expose Retry-After。

客户端跨源（桌面 SPA origin=app://weave → http://127.0.0.1:<协商端口>）读不到
非 CORS 安全清单响应头——drain 门 503 的 Retry-After 被浏览器屏蔽，客户端恒回退
内置 5000ms。修法=main.py CORSMiddleware `expose_headers=["Retry-After"]`；
本测钉真接线（main.app）而非配置复刻。

Run: cd backend && python -m pytest tests/test_cors_expose_headers.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _client():
    from fastapi.testclient import TestClient

    from main import app
    return TestClient(app)


def test_cross_origin_response_exposes_retry_after():
    """跨源（带 Origin）响应必须含 Access-Control-Expose-Headers: Retry-After。"""
    r = _client().get("/", headers={"Origin": "app://weave"})
    assert r.status_code == 200
    expose = r.headers.get("access-control-expose-headers", "")
    assert "retry-after" in expose.lower(), (
        f"跨源响应必须 expose Retry-After（drain 503 退避依赖），实得 {expose!r}"
    )


def test_error_response_also_carries_expose_header():
    """非 200（404 路由）同样经中间件——expose 头不因错误面丢失。"""
    r = _client().get("/api/upstream-20261007-nonexistent",
                      headers={"Origin": "app://weave"})
    assert r.status_code == 404
    expose = r.headers.get("access-control-expose-headers", "")
    assert "retry-after" in expose.lower(), f"错误面也必须带 expose 头，实得 {expose!r}"
