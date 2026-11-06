"""TDD tests for model_gateway gateway facade (统一模型调用门面).

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_model_gateway_gateway.py -v
"""
import pytest

from tests.test_model_gateway_registry import CONF, _registry


def _gateway(reg=None):
    from app.model_gateway.gateway import ModelGateway
    return ModelGateway(registry=reg or _registry())


class TestKindRouting:
    def test_chat_uses_llm_factory_with_main_endpoint(self, monkeypatch):
        gw = _gateway()
        seen = {}

        class _FakeLLM:
            async def complete_chat(self, messages, **kw):
                return "ok"

        def fake_build(ep, preserve_reasoning=False):
            seen["ep"] = ep
            return _FakeLLM()

        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", fake_build)
        out = gw.chat_sync([{"role": "user", "content": "hi"}])
        assert out == "ok"
        assert seen["ep"].alias == "main"
        assert seen["ep"].kind == "llm"

    def test_chat_on_non_llm_alias_raises(self):
        gw = _gateway()
        with pytest.raises(TypeError):
            gw.chat_sync([{"role": "user", "content": "hi"}], model="embedding")

    def test_unknown_alias_raises_keyerror(self):
        gw = _gateway()
        with pytest.raises(KeyError):
            gw.chat_sync([{"role": "user", "content": "hi"}], model="ghost")

    def test_purpose_routing(self, monkeypatch):
        gw = _gateway()
        seen = {}

        class _FakeLLM:
            async def complete_chat(self, messages, **kw):
                return "ok"

        def fake_build(ep, preserve_reasoning=False):
            seen["ep"] = ep
            return _FakeLLM()

        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", fake_build)
        gw.chat_sync([], purpose="memory.concept_extraction")
        assert seen["ep"].alias == "deepseek"

    def test_embed_routes_to_embedding_kind(self, monkeypatch):
        gw = _gateway()
        seen = {}

        async def fake_embed(ep, texts):
            seen["ep"] = ep
            return [[0.1]]

        monkeypatch.setattr(gw, "_call_embed", fake_embed)
        out = gw.embed_sync(["hello"])
        assert out == [[0.1]]
        assert seen["ep"].kind == "embedding"

    def test_rerank_routes_to_rerank_kind(self, monkeypatch):
        gw = _gateway()
        seen = {}

        async def fake_rerank(ep, query, documents, top_n, timeout):
            seen["ep"] = ep
            return [{"index": 0, "relevance_score": 0.9}]

        monkeypatch.setattr(gw, "_call_rerank", fake_rerank)
        out = gw.rerank_sync("q", ["d1"])
        assert out[0]["relevance_score"] == 0.9
        assert seen["ep"].kind == "rerank"

    def test_transcribe_routes_to_asr_kind(self, monkeypatch):
        gw = _gateway()
        seen = {}

        async def fake_asr(ep, audio_data, filename, **kw):
            seen["ep"] = ep
            return {"text": "你好"}

        monkeypatch.setattr(gw, "_call_transcribe", fake_asr)
        out = gw.transcribe_sync(b"pcm", "a.wav")
        assert out["text"] == "你好"
        assert seen["ep"].kind == "asr"

    def test_synthesize_routes_to_tts_kind(self, monkeypatch):
        gw = _gateway()
        seen = {}

        def fake_tts(ep, text, **kw):
            seen["ep"] = ep
            return iter([b"chunk"])

        monkeypatch.setattr(gw, "_call_synthesize", fake_tts)
        chunks = list(gw.synthesize_sync("你好"))
        assert chunks == [b"chunk"]
        assert seen["ep"].kind == "tts"


class TestFactory:
    def test_build_llm_service_from_endpoint(self):
        # 2026-10-18: hermetic — the old assertion hardcoded the operator's
        # live `main` URL (DeepSeek), which broke whenever `main` moved to
        # MiMo. The factory contract is "preserve the endpoint's URL and
        # is_custom flag", not "main is DeepSeek".
        from app.model_gateway import factory
        from app.model_gateway.schemas import KIND_LLM, ModelEndpoint
        ep = ModelEndpoint(
            alias="main", kind=KIND_LLM,
            base_url="https://api.deepseek.com/v1",
            api_key="test-key", model_name="deepseek-flash",
            is_custom=False,
        )
        svc = factory.build_llm_service(ep)
        # is_custom=False keeps the legacy bare-constructor semantics: the
        # global [api] base URL, not the endpoint's base_url. Assert the
        # contract, not the operator's current main provider.
        assert svc.is_custom_provider is False
        assert svc.endpoint is ep
        assert str(svc.client.base_url).rstrip("/") == str(svc.config.api_base_url).rstrip("/")

    def test_build_llm_service_custom_endpoint(self):
        from app.model_gateway import factory
        reg = _registry()
        svc = factory.build_llm_service(reg.get("qwen3.8_27b"))
        assert str(svc.client.base_url).rstrip("/") == "http://biklimax.cn:28000/v1"
        assert svc.is_custom_provider is True
        assert svc.custom_model_name == "qwen3.8-27b"
