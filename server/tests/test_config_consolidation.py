"""TDD tests: 模型配置两文件合并——registry 从合并配置（config_model.toml 的
[endpoints.*]/[routing] 段）加载，legacy 键合成保留为回滚网（2026-08-31）.

Criteria map: tests/acceptance.md items 22, 23, 24.
Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_config_consolidation.py -v
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.model_gateway.registry import ModelRegistry, synthesize


CONF_LEGACY = {
    "api": {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-main", "model_name": "deepseek-chat"},
    "defaults": {"temperature": 0.7},
    "providers": {
        "mimo": {"base_url": "http://mimo:9000/v1", "api_key": "sk-m", "model_name": "mimo-v2.5", "type": "mimo"},
    },
}


# ── Criterion 22: endpoints/routing from the merged config dict ──────────

def test_endpoints_from_merged_config():
    conf = dict(CONF_LEGACY)
    conf["endpoints"] = {
        "custom-x": {"kind": "llm", "base_url": "http://x/v1", "api_key": "sk-x",
                     "model_name": "x-1", "provider_type": "openai", "is_custom": True},
    }
    reg = ModelRegistry.from_sources(conf)
    ep = reg.get("custom-x")
    assert ep.base_url == "http://x/v1" and ep.api_key == "sk-x" and ep.model_name == "x-1"


def test_routing_from_merged_config():
    conf = dict(CONF_LEGACY)
    conf["endpoints"] = {"custom-x": {"kind": "llm", "base_url": "http://x/v1",
                                      "api_key": "sk-x", "model_name": "x-1"}}
    conf["routing"] = {"judge": "custom-x", "deathmatch.judge": {"alias": "custom-x", "is_custom": True}}
    reg = ModelRegistry.from_sources(conf)
    assert reg.resolve("judge").alias == "custom-x"
    assert reg.resolve("deathmatch.judge").alias == "custom-x"


def test_explicit_sections_beat_synthesized():
    """Explicit [endpoints.main] overrides the synthesized main endpoint."""
    conf = dict(CONF_LEGACY)
    conf["endpoints"] = {"main": {"kind": "llm", "base_url": "http://override/v1",
                                  "api_key": "sk-o", "model_name": "o-1"}}
    reg = ModelRegistry.from_sources(conf)
    assert reg.get("main").base_url == "http://override/v1"
    assert reg.get("main").model_name == "o-1"


# ── Criterion 23: no endpoints section → legacy keys still synthesize ────

def test_legacy_synthesize_still_works():
    reg = ModelRegistry.from_sources(CONF_LEGACY)
    assert reg.get("main").model_name == "deepseek-chat"
    assert reg.get("mimo").base_url == "http://mimo:9000/v1"
    # vlm placeholder still synthesized (empty = unconfigured)
    assert reg.has("vlm") and not reg.get("vlm").base_url


# ── Structural: the toml-file loading path is gone ───────────────────────

def test_from_sources_has_no_toml_param():
    sig = inspect.signature(ModelRegistry.from_sources)
    assert "toml_path" not in sig.parameters, (
        "from_sources must read endpoints/routing from the merged config only"
    )


def test_no_default_registry_toml_path_symbol():
    import app.model_gateway.registry as reg_mod
    assert not hasattr(reg_mod, "default_registry_toml_path"), (
        "default_registry_toml_path must be removed (config_model.toml is the single source)"
    )


def test_reload_clears_config_cache(monkeypatch):
    """reload_model_registry must clear the config cache first — endpoints
    now ride the merged config, so a stale cache would serve stale endpoints."""
    import app.model_gateway.registry as reg_mod
    calls = []
    monkeypatch.setattr("app.core.config.clear_config_cache", lambda: calls.append(1))
    monkeypatch.setattr(reg_mod, "get_model_registry", lambda: object())
    reg_mod.reload_model_registry()
    assert calls == [1], "clear_config_cache must be called exactly once"


# ── Criterion 24: vlm compat via merged endpoints section ────────────────

def test_vlm_legacy_endpoint_compat_via_conf():
    conf = {"endpoints": {"deathmatch.vlm": {"kind": "llm", "base_url": "http://legacy/v1",
                                             "api_key": "sk-l", "model_name": "mm-1"}}}
    reg = ModelRegistry.from_sources(conf)
    ep = reg.resolve("vlm")
    assert ep.alias == "deathmatch.vlm" and ep.base_url == "http://legacy/v1"
