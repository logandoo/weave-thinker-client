"""TDD tests for model_gateway registry (模型网关注册表).

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_model_gateway_registry.py -v
"""
import textwrap
from types import SimpleNamespace

import pytest


CONF = {
    "api": {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-main", "model_name": "deepseek-chat"},
    "defaults": {"temperature": 0.7, "top_p": 1.0, "max_tokens": 4096},
    "providers": {
        "qwen3.8_27b": {"base_url": "http://biklimax.cn:28000/v1", "api_key": "sk-q", "model_name": "qwen3.8-27b", "type": "openai"},
        "mimo": {"base_url": "http://mimo:9000/v1", "api_key": "", "model_name": "mimo-v2.5-pro", "type": "mimo"},
    },
    "asr": {
        "base_url": "http://asr:8000", "model": "qwen3-asr", "is_dashscope": False,
        "is_mimo": False, "api_key": "", "dashscope_api_key": "sk-ds",
        "dashscope_model": "qwen3-asr-flash-realtime-2026-02-10", "websocket_url": "",
    },
    "voice": {
        "provider": "mimo", "model_name": "mimo-v2.5-pro",
        "tts_base_url": "http://mimo:9000/v1", "tts_api_key": "k-tts",
        "tts_model": "mimo-v2.5-tts", "tts_voice": "冰糖",
    },
    "memory": {
        "embedding_api_base": "http://emb:28005/v1", "embedding_api_key": "sk-e",
        "embedding_model": "bge-m3", "embedding_dim": 1024,
        "rerank_api_base": "http://rr:28007", "rerank_api_key": "sk-r",
        "rerank_model": "bge-reranker-v2-m3",
        "concept_extraction_model": "deepseek", "dream_model": "",
    },
    "deathmatch": {"judge": {"base_url": "", "api_key": "", "model_name": ""}},
    "agent": {
        "auxiliary": {"coordinator_model": "deepseek-v4-flash", "compression_model": ""},
        "moa": {"reference_providers": ["qwen3.8_27b"], "aggregator_provider": "deepseek"},
    },
}


def _registry(conf=CONF):
    from app.model_gateway.registry import ModelRegistry
    return ModelRegistry.from_sources(conf)


class TestSynthesizeFromConfig:
    def test_main_endpoint_from_api_section(self):
        reg = _registry()
        main = reg.get("main")
        assert main.kind == "llm"
        assert main.base_url == "https://api.deepseek.com/v1"
        assert main.api_key == "sk-main"
        assert main.model_name == "deepseek-chat"
        # bare-LLMService 语义：应用 [defaults] 全局采样（is_custom=False）
        assert main.is_custom is False
        assert main.params.get("temperature") == 0.7

    def test_provider_sections_become_aliases(self):
        reg = _registry()
        q = reg.get("qwen3.8_27b")
        assert q.kind == "llm"
        assert q.base_url == "http://biklimax.cn:28000/v1"
        assert q.is_custom is True
        # provider_type 别名映射：qwen3.8_vllm → qwen3.8_27b
        assert reg.get("qwen3.8_vllm").base_url == q.base_url

    def test_deepseek_alias_synthesized_from_api(self):
        # provider_configs 的既有 fallback：deepseek 缺省回落 [api]，但语义=is_custom
        reg = _registry()
        ds = reg.get("deepseek")
        assert ds.base_url == "https://api.deepseek.com/v1"
        assert ds.is_custom is True

    def test_non_llm_endpoints(self):
        reg = _registry()
        emb = reg.get("embedding")
        assert emb.kind == "embedding"
        assert emb.base_url == "http://emb:28005/v1"
        assert emb.model_name == "bge-m3"
        assert emb.extra.get("dim") == 1024
        rr = reg.get("rerank")
        assert rr.kind == "rerank"
        assert rr.model_name == "bge-reranker-v2-m3"
        asr = reg.get("asr")
        assert asr.kind == "asr"
        assert asr.extra.get("dashscope_api_key") == "sk-ds"
        tts = reg.get("tts")
        assert tts.kind == "tts"
        assert tts.model_name == "mimo-v2.5-tts"
        assert tts.extra.get("voice") == "冰糖"

    def test_unknown_alias_raises(self):
        reg = _registry()
        with pytest.raises(KeyError):
            reg.get("nonexistent")


class TestRouting:
    def test_purpose_fallback_to_main(self):
        reg = _registry()
        assert reg.resolve("judge").alias == "main"
        assert reg.resolve("some.unknown.purpose").alias == "main"

    def test_memory_model_routes_to_provider_alias(self):
        reg = _registry()
        ep = reg.resolve("memory.concept_extraction")
        assert ep.alias == "deepseek"
        assert ep.base_url == "https://api.deepseek.com/v1"

    def test_aux_model_name_override_on_main(self):
        # coordinator_model 是主端点上的 model_name 覆盖（不是 provider 别名）；
        # legacy resolve_aux_model_context 显式传 url/key → is_custom=True 语义
        reg = _registry()
        ep = reg.resolve("coordinator")
        assert ep.alias == "main"
        assert ep.model_name == "deepseek-v4-flash"
        assert ep.is_custom is True
        # 未配置 override 的 purpose 不覆盖（aux.* 走 AuxiliaryClient 裸构造语义 is_custom=False）
        assert reg.resolve("aux.compression").model_name == "deepseek-chat"
        assert reg.resolve("aux.compression").is_custom is False

    def test_voice_purpose_uses_voice_provider(self):
        reg = _registry()
        ep = reg.resolve("voice")
        assert ep.alias == "mimo"
        assert ep.model_name == "mimo-v2.5-pro"


class TestCapabilities:
    """思考菜单 capabilities：数组序=显示序；effort_meta=模型层显式配置项。"""

    def test_deepseek_efforts_desc_order_and_meta(self):
        reg = _registry()
        caps = reg.get("deepseek").capabilities
        assert caps["supports_reasoning"] is True
        assert caps["reasoning_efforts"] == ["max", "high", "low"]  # legacy 降序
        meta = caps["effort_meta"]
        assert meta["max"]["label"] == "Max"
        assert meta["high"]["label"] == "High" and meta["high"]["default"] is True
        assert meta["low"]["label"] == "Low"
        assert meta["high"]["desc"] == "标准深度思考"

    def test_qwen38_efforts_lowercase_labels(self):
        reg = _registry()
        caps = reg.get("qwen3.8_27b").capabilities
        assert caps["reasoning_efforts"] == ["xhigh", "medium", "low"]
        meta = caps["effort_meta"]
        assert meta["xhigh"]["label"] == "xhigh"
        assert meta["medium"]["label"] == "medium"
        assert meta["low"]["label"] == "low"
        assert "default" not in meta["xhigh"] and "default" not in meta["medium"] and "default" not in meta["low"]

    def test_doubao_efforts_no_minimal(self):
        # 2026-10-04 用户裁定：Doubao minimal 档与「关闭」重复 → 档位移除。
        from app.model_gateway.registry import _capabilities_for
        caps = _capabilities_for("doubao")
        assert caps["supports_reasoning"] is True
        assert caps["reasoning_efforts"] == ["high", "medium", "low"]
        assert "minimal" not in caps["effort_meta"]
        assert caps["effort_meta"]["medium"]["default"] is True

    def test_stale_toml_efforts_clamped_to_wire_legal(self):
        # A4.9 R1 Important：遗留 config_model.toml 的 stale 档位不得穿到菜单。
        conf = dict(CONF)
        conf["endpoints"] = {
            "doubao-old": {
                "kind": "llm", "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "api_key": "sk-x", "model_name": "doubao-seed-evolving",
                "provider_type": "doubao",
                "capabilities": {
                    "supports_reasoning": True,
                    "reasoning_efforts": ["high", "medium", "low", "minimal"],
                    "effort_meta": {"minimal": {"label": "minimal", "desc": "关闭思考，直接回答"},
                                    "high": {"label": "high", "desc": "深度分析，处理复杂问题"}},
                },
            }
        }
        reg = _registry(conf)
        caps = reg.get("doubao-old").capabilities
        assert caps["reasoning_efforts"] == ["high", "medium", "low"]
        assert "minimal" not in caps["effort_meta"]
        assert "high" in caps["effort_meta"]
        # generic/custom（efforts=None）不钳
        conf["endpoints"]["custom-x"] = {
            "kind": "llm", "base_url": "http://x/v1", "api_key": "", "model_name": "m",
            "provider_type": "custom",
            "capabilities": {"reasoning_efforts": ["weird"], "effort_meta": {}},
        }
        assert _registry(conf).get("custom-x").capabilities["reasoning_efforts"] == ["weird"]

    def test_effort_meta_params_pass_through_toml(self):
        conf = dict(CONF)
        conf["endpoints"] = {
            "ds": {
                "kind": "llm", "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-x", "model_name": "deepseek-chat",
                "provider_type": "deepseek",
                "capabilities": {
                    "supports_reasoning": True,
                    "reasoning_efforts": ["max", "high", "low"],
                    "effort_meta": {"max": {"label": "Max", "desc": "最大深度思考",
                                            "params": {"thinking_budget": 65536}}},
                },
            }
        }
        reg = _registry(conf)
        caps = reg.get("ds").capabilities
        assert caps["effort_meta"]["max"]["params"]["thinking_budget"] == 65536


class TestExplicitSectionsOverride:
    """显式 [endpoints.*]/[routing] 段（合并配置，2026-08-31 两文件合并）覆盖合成表。"""

    def _conf(self):
        conf = dict(CONF)
        conf["endpoints"] = {
            "main": {
                "kind": "llm", "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-override", "model_name": "deepseek-v4-flash",
                "display_name": "DeepSeek V4",
                "capabilities": {"supports_reasoning": True,
                                 "reasoning_efforts": ["low", "high", "max"]},
            },
            "local-qwen": {
                "kind": "llm", "base_url": "http://127.0.0.1:8000/v1",
                "api_key": "", "model_name": "qwen-local",
            },
        }
        conf["routing"] = {"judge": "local-qwen"}
        return conf

    def test_explicit_overrides_and_extends(self):
        reg = _registry(self._conf())
        main = reg.get("main")
        assert main.api_key == "sk-override"
        assert main.model_name == "deepseek-v4-flash"
        assert main.display_name == "DeepSeek V4"
        assert main.capabilities.get("supports_reasoning") is True
        assert reg.get("local-qwen").model_name == "qwen-local"
        # config 合成的端点仍在（显式段未覆盖）
        assert reg.get("embedding").kind == "embedding"

    def test_explicit_routing(self):
        reg = _registry(self._conf())
        assert reg.resolve("judge").alias == "local-qwen"

    def test_missing_sections_is_not_an_error(self):
        reg = _registry()
        assert reg.get("main").kind == "llm"


class TestPublicSerialization:
    def test_public_view_strips_secrets(self):
        reg = _registry()
        pub = reg.public_aliases()
        assert pub, "expected at least one public alias"
        for item in pub:
            assert "api_key" not in item
            assert "base_url" not in item
            assert "model_name" not in item
            assert "alias" in item and "kind" in item
            assert "capabilities" in item
        aliases = {i["alias"] for i in pub}
        assert "main" in aliases

    def test_public_view_kind_filter(self):
        reg = _registry()
        llm_only = reg.public_aliases(kind="llm")
        assert all(i["kind"] == "llm" for i in llm_only)
        kinds = {i["kind"] for i in reg.public_aliases()}
        assert {"llm", "embedding", "rerank", "asr", "tts"} <= kinds


class TestEndpointForAssistant:
    def _assistant(self, **kw):
        defaults = dict(
            model_alias=None, provider_type="deepseek",
            custom_api_url=None, custom_api_key=None, custom_model_name=None,
            use_custom_model=False,
        )
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def test_model_alias_wins(self):
        reg = _registry()
        a = self._assistant(model_alias="qwen3.8_27b", provider_type="deepseek")
        assert reg.endpoint_for_assistant(a).alias == "qwen3.8_27b"

    def test_unknown_model_alias_falls_back_main(self):
        reg = _registry()
        a = self._assistant(model_alias="ghost")
        assert reg.endpoint_for_assistant(a).alias == "main"

    def test_legacy_custom_row_inline_endpoint(self):
        reg = _registry()
        a = self._assistant(
            provider_type="custom",
            custom_api_url="http://my-llm:1234/v1",
            custom_api_key="sk-mine",
            custom_model_name="my-model",
        )
        ep = reg.endpoint_for_assistant(a)
        assert ep.base_url == "http://my-llm:1234/v1"
        assert ep.api_key == "sk-mine"
        assert ep.model_name == "my-model"
        assert ep.is_custom is True

    def test_legacy_deepseek_assistant(self):
        reg = _registry()
        a = self._assistant(provider_type="deepseek")
        ep = reg.endpoint_for_assistant(a)
        assert ep.base_url == "https://api.deepseek.com/v1"
        assert ep.is_custom is True

    def test_none_assistant_gives_main(self):
        reg = _registry()
        assert reg.endpoint_for_assistant(None).alias == "main"
