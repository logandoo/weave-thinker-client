"""A4.7 API tests (from API.md) — 模型别名端点 + 助手模型配置剥离。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_models_api.py -v
Requires the 8158 server running (scripts/start.sh, SSL self-signed).
"""
import uuid

import httpx
import pytest

BASE = "https://localhost:8158"
FORBIDDEN_MODEL_FIELDS = {
    "custom_api_key", "custom_api_url", "custom_model_name", "provider_type",
    "use_custom_model", "extra_body", "subtask_custom_api_url",
    "subtask_custom_api_key", "subtask_custom_model_name",
    "subtask_provider_type", "subtask_extra_body", "use_subtask_model",
    "temperature", "top_p", "top_k", "presence_penalty", "frequency_penalty",
    "max_tokens", "thinking_budget", "min_p", "repetition_penalty",
    "thinking_temperature", "thinking_top_p", "thinking_top_k",
    "thinking_min_p", "thinking_presence_penalty",
    "thinking_repetition_penalty", "preserve_thinking",
}


@pytest.fixture(scope="module")
def token():
    r = httpx.post(f"{BASE}/api/auth/login",
                   json={"username": "test", "password": "123456"},
                   verify=False, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def client(token):
    return httpx.Client(base_url=BASE, verify=False, timeout=15,
                        headers={"Authorization": f"Bearer {token}"})


class TestModelsEndpoint:
    def test_requires_auth(self):
        r = httpx.get(f"{BASE}/api/models", verify=False, timeout=10)
        assert r.status_code in (401, 403)

    def test_lists_aliases_without_secrets(self, client):
        r = client.get("/api/models")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "aliases" in data and "default_alias" in data
        assert isinstance(data["aliases"], list) and data["aliases"], "expected >=1 alias"
        raw = r.text
        assert "api_key" not in raw
        assert "base_url" not in raw
        assert "model_name" not in raw
        for item in data["aliases"]:
            assert set(item) <= {"alias", "kind", "display_name", "capabilities"}, item
            assert item["kind"] in ("llm", "embedding", "rerank", "asr", "tts")
        llm_aliases = [a["alias"] for a in data["aliases"] if a["kind"] == "llm"]
        assert data["default_alias"] in llm_aliases

    def test_old_providers_endpoint_gone(self, client):
        r = client.get("/api/config/providers")
        assert r.status_code == 404


class TestAssistantModelAlias:
    def test_list_strips_model_config_fields(self, client):
        r = client.get("/api/assistants")
        assert r.status_code == 200, r.text
        items = r.json()
        assert items, "expected at least one assistant"
        for a in items:
            leaked = FORBIDDEN_MODEL_FIELDS & set(a)
            assert not leaked, f"assistant response leaks model fields: {leaked}"
            assert "model_alias" in a
            assert "subtask_model_alias" in a

    def test_create_with_alias_and_roundtrip(self, client):
        name = f"gw-alias-{uuid.uuid4().hex[:8]}"
        r = client.post("/api/assistants", json={
            "name": name,
            "system_prompt": "test",
            "model_alias": "deepseek",
            # 旧字段写入应被静默忽略（向后兼容）
            "temperature": 0.5,
            "custom_api_key": "should-be-ignored",
        })
        assert r.status_code == 200, r.text
        created = r.json()
        assert created["model_alias"] == "deepseek"
        assert not (FORBIDDEN_MODEL_FIELDS & set(created))
        aid = created["id"]
        try:
            r2 = client.get(f"/api/assistants/{aid}")
            assert r2.status_code == 200
            body = r2.json()
            assert body["model_alias"] == "deepseek"
            assert "custom_api_key" not in body
            # PUT 更新别名
            r3 = client.put(f"/api/assistants/{aid}", json={"model_alias": "main"})
            assert r3.status_code == 200, r3.text
            assert r3.json()["model_alias"] == "main"
        finally:
            client.delete(f"/api/assistants/{aid}")

    def test_unknown_alias_rejected(self, client):
        r = client.post("/api/assistants", json={
            "name": f"gw-ghost-{uuid.uuid4().hex[:8]}",
            "model_alias": "ghost-alias-does-not-exist",
        })
        assert r.status_code == 422, r.text
