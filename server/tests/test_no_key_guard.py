"""Empty-endpoint-key guard ("no-key" sentinel) — wave2 2026-09-01.

An explicitly-configured endpoint (base_url filled) with api_key="" must wire
the harmless "no-key" placeholder, NEVER the global main key (which would leak
the DeepSeek credential into keyless local/sidecar servers' auth headers).
Regression coverage for the review-driven guard class: factory (LLM),
memory_embedding_service, tts_service, moa_service.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.model_gateway.factory import build_llm_service  # noqa: E402
from app.model_gateway.schemas import ModelEndpoint  # noqa: E402


def _ep(**kw):
    base = dict(alias="local", kind="llm", base_url="http://127.0.0.1:9/v1",
                api_key="", model_name="m", is_custom=True)
    base.update(kw)
    return ModelEndpoint(**base)


def test_custom_llm_empty_key_wires_no_key_sentinel():
    svc = build_llm_service(_ep())
    assert svc.client.api_key == "no-key"


def test_custom_llm_real_key_passthrough():
    svc = build_llm_service(_ep(api_key="sk-real"))
    assert svc.client.api_key == "sk-real"


def test_bare_endpoint_keeps_global_fallback():
    # base_url 为空（裸 LLMService 语义）→ 传 None，llm_service 内部回落全局 key
    svc = build_llm_service(_ep(base_url="", api_key=""))
    # None → llm_service 内部回落全局 key（裸构造语义保持）
    assert svc.client.api_key == svc.config.api_key


def test_embedding_empty_key_wires_no_key_sentinel(monkeypatch):
    import app.services.memory_embedding_service as mes
    ep = ModelEndpoint(alias="embedding", kind="embedding",
                       base_url="http://127.0.0.1:9/v1", api_key="", model_name="e")
    monkeypatch.setattr(mes, "_embedding_endpoint", lambda: ep)
    assert mes._get_embedding_api_key() == "no-key"


def test_embedding_no_base_url_falls_back_to_global(monkeypatch):
    import app.services.memory_embedding_service as mes
    ep = ModelEndpoint(alias="embedding", kind="embedding",
                       base_url="", api_key="", model_name="e")
    monkeypatch.setattr(mes, "_embedding_endpoint", lambda: ep)
    assert mes._get_embedding_api_key() == mes.config.api_key


def test_tts_empty_key_wires_no_key_sentinel():
    from app.services.tts_service import TTSService
    svc = TTSService()
    ep = ModelEndpoint(alias="tts", kind="tts", base_url="http://127.0.0.1:9/v1",
                       api_key="", model_name="t")
    original = type(svc)._endpoint
    type(svc)._endpoint = lambda self: ep
    try:
        assert svc._resolve_api_key() == "no-key"
    finally:
        type(svc)._endpoint = original


def test_moa_keyless_reference_gets_sentinel_not_main_key():
    # 真实注册表：qwen3.8 端点 base_url 已填 + api_key="" → 必须是 "no-key"，
    # 绝不是全局 main key（DeepSeek sk-…）
    from app.services.moa_service import MoAService
    kwargs, _ = MoAService._endpoint_kwargs("qwen3.8")
    assert kwargs["api_key"] == "no-key"
    assert not str(kwargs["api_key"]).startswith("sk-")
