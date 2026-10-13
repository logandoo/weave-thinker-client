"""TDD tests — 登录滑动续期：token 有效期配置化 + 签发时长（wave-3）。

现状：auth_service.ACCESS_TOKEN_EXPIRE_DAYS = 7 硬编码（不在 config）。
目标：`[security] token_expire_days`（默认 7）驱动 create_access_token 的
exp−iat。refresh 端点的行为由 tests/api/test_auth_refresh.py（对 8158 实跑）
与前端 Playwright spec 覆盖。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_auth_refresh.py -v
"""
from datetime import datetime

import jwt

from app.core.config import get_config
from app.services.auth_service import create_access_token, SECRET_KEY


def test_config_token_expire_days_default():
    cfg = get_config()
    assert cfg.security_token_expire_days == 7


def test_token_expire_days_clamped():
    """A 0/negative window would issue immediately-dead tokens (login OK →
    401 loop); non-numeric falls back to 7 (A4.9 I2)."""
    cfg = get_config()
    saved = cfg._config.get("security", {}).get("token_expire_days")
    try:
        for bad in (0, -5):
            cfg._config.setdefault("security", {})["token_expire_days"] = bad
            assert cfg.security_token_expire_days == 1, f"{bad} 应钳制为 1"
        for fallback in (float("inf"), "abc"):
            cfg._config["security"]["token_expire_days"] = fallback
            assert cfg.security_token_expire_days == 7, f"{fallback!r} 应回退 7"
    finally:
        if saved is None:
            cfg._config.get("security", {}).pop("token_expire_days", None)
        else:
            cfg._config["security"]["token_expire_days"] = saved


def _token_duration_days(token):
    payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    return (payload["exp"] - payload["iat"]) / 86400


def test_token_duration_follows_config():
    cfg = get_config()
    t = create_access_token("u-1", "user1")
    got = _token_duration_days(t)
    assert abs(got - cfg.security_token_expire_days) < 1 / 86400, \
        f"默认应 {cfg.security_token_expire_days} 天, 实得 {got}"

    saved = cfg._config.get("security", {}).get("token_expire_days")
    cfg._config.setdefault("security", {})["token_expire_days"] = 30
    try:
        t30 = create_access_token("u-1", "user1")
        assert abs(_token_duration_days(t30) - 30) < 1 / 86400, \
            "配置 30 天后签发时长必须 = 30 天（硬编码不读配置则失败）"
    finally:
        if saved is None:
            cfg._config["security"].pop("token_expire_days", None)
        else:
            cfg._config["security"]["token_expire_days"] = saved
