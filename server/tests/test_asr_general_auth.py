"""通用 HTTP ASR 路径鉴权头（A4.9 R2 复审 N3 回归）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_asr_general_auth.py -v
"""
from app.services.asr_service import _general_auth_headers


def test_user_key_sent_as_bearer():
    assert _general_auth_headers("unit-asr-key") == {"Authorization": "Bearer unit-asr-key"}


def test_empty_key_sends_no_header():
    assert _general_auth_headers("") == {}
    assert _general_auth_headers(None) == {}
