"""P0 S2: production startup guard (local dev config stays untouched)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Config, production_security_errors

VALID_JWT = "x" * 64


def _config(tmp_path, *, bypass=False, jwt="dev-secret-key-change-in-production-weave-thinker-2026", db_host="127.0.0.1"):
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[server]
host = "0.0.0.0"
port = 8158

[security]
jwt_secret_key = "{jwt}"
super_admin_bypass = {str(bypass).lower()}

[database]
host = "{db_host}"
port = 5432
username = "u"
password = "p"
name = "n"
""".strip(),
        encoding="utf-8",
    )
    return Config(str(path))


def test_production_rejects_dev_jwt_bypass_and_localhost_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WT_ENV", "production")
    errors = production_security_errors(_config(tmp_path, bypass=True))
    joined = " | ".join(errors)
    assert "super_admin_bypass" in joined
    assert "jwt" in joined.lower()
    assert "localhost" in joined.lower() or "127.0.0.1" in joined


def test_production_accepts_hardened_config(tmp_path, monkeypatch):
    monkeypatch.setenv("WT_ENV", "production")
    errors = production_security_errors(
        _config(tmp_path, bypass=False, jwt=VALID_JWT, db_host="db")
    )
    assert errors == []


def test_local_dev_is_not_blocked(tmp_path, monkeypatch):
    monkeypatch.delenv("WT_ENV", raising=False)
    assert production_security_errors(_config(tmp_path)) == []
    monkeypatch.setenv("WT_ENV", "development")
    assert production_security_errors(_config(tmp_path)) == []
