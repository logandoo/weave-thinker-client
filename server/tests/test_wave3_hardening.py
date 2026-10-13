"""TDD tests for wave-3 hardening (A4.9 R3 deferred minors + legacy/voice 链路).

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_wave3_hardening.py -v
"""
from types import SimpleNamespace

import pytest

from app.model_gateway.schemas import ModelEndpoint


class TestEndpointParamsMaxTokensFork:
    """R3 Minor①：endpoint params max_tokens=0/"" 必须与 kwargs 同一归一化
    （视为未设置并回落 [defaults]，而不是抑制后吞掉回落）。"""

    def _svc(self, params):
        from app.model_gateway import factory
        ep = ModelEndpoint(alias="t-ep", kind="llm", base_url="http://x:1/v1",
                           api_key="k", model_name="m", is_custom=True, params=params)
        return factory.build_llm_service(ep)

    def test_zero_never_on_wire_and_no_fork_vs_kwargs(self, monkeypatch):
        # custom 端点：max_tokens=0 与 kwargs 侧 0 结果必须一致——都不上 wire
        # （legacy 对 custom 从不应用 [defaults]，故正确不变量是"无分叉"）
        svc = self._svc({"max_tokens": 0})
        monkeypatch.setattr(type(svc.config), "default_max_tokens",
                            property(lambda self: 4321))
        ep_side = svc._build_params([{"role": "user", "content": "hi"}])
        kw_side = svc._build_params([{"role": "user", "content": "hi"}], max_tokens=0)
        assert "max_tokens" not in ep_side, f"endpoint max_tokens=0 不应上 wire: {ep_side.get('max_tokens')!r}"
        assert "max_tokens" not in kw_side
        assert ep_side.get("max_tokens") == kw_side.get("max_tokens")

    def test_empty_string_never_on_wire(self):
        svc = self._svc({"max_tokens": ""})
        params = svc._build_params([{"role": "user", "content": "hi"}])
        assert "max_tokens" not in params

    def test_string_zero_never_on_wire(self):
        # TOML 引号零 "0" 与数值 0 同义（TOML 卫生）——不得上 wire
        svc = self._svc({"max_tokens": "0"})
        params = svc._build_params([{"role": "user", "content": "hi"}])
        assert "max_tokens" not in params

    def test_positive_value_on_wire(self):
        svc = self._svc({"max_tokens": 2048})
        params = svc._build_params([{"role": "user", "content": "hi"}])
        assert params.get("max_tokens") == 2048


class TestFallbackWireType:
    """R3 Minor②：fallback wire type 不得退化为别名串（非类型）。"""

    def test_typeless_endpoint_gives_neutral_custom(self):
        from app.model_gateway import factory
        ep = ModelEndpoint(alias="qwen-local", kind="llm", base_url="http://x:1/v1")
        assert factory.fallback_wire_type(ep) == "custom"

    def test_typed_endpoint_keeps_type(self):
        from app.model_gateway import factory
        ep = ModelEndpoint(alias="q", kind="llm", base_url="http://x:1/v1",
                           provider_type="qwen3.8_vllm")
        assert factory.fallback_wire_type(ep) == "qwen3.8_vllm"


class TestStaleAliasSerialization:
    """R3 Minor③：Response 序列化对陈旧别名必须容错（列表端点不得 500）；
    Create/Update 的严格校验不变。"""

    def test_response_tolerates_stale_alias(self):
        from app.schemas.assistant import AssistantResponse
        r = AssistantResponse(
            id="a1", user_id="u1", name="x", system_prompt="",
            model_alias="ghost-removed-from-registry",
            subtask_model_alias="",
            created_at="2026-08-30T00:00:00", updated_at="2026-08-30T00:00:00",
        )
        assert r.model_alias == "ghost-removed-from-registry"

    def test_response_tolerates_stale_subtask_alias(self):
        from app.schemas.assistant import AssistantResponse
        r = AssistantResponse(
            id="a1", user_id="u1", name="x", system_prompt="",
            model_alias="deepseek",
            subtask_model_alias="ghost-removed-from-registry",
            created_at="2026-08-30T00:00:00", updated_at="2026-08-30T00:00:00",
        )
        assert r.subtask_model_alias == "ghost-removed-from-registry"

    def test_create_still_rejects_unknown_alias(self):
        from app.schemas.assistant import AssistantCreate
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            AssistantCreate(name="x", model_alias="ghost-does-not-exist")

    def test_create_still_rejects_unknown_subtask_alias(self):
        # R2 Minor3：subtask 的 Create 严格性同样需要测试钉住
        from app.schemas.assistant import AssistantCreate
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            AssistantCreate(name="x", subtask_model_alias="ghost-does-not-exist")

    def test_create_empty_alias_means_server_default(self):
        # wave-6 统一性：前端不再持有供应商缺省名——空别名=服务端默认
        from app.schemas.assistant import AssistantCreate
        c = AssistantCreate(name="x", model_alias="", subtask_model_alias="")
        assert c.model_alias == "" and c.subtask_model_alias == ""


class TestVoiceBuildLlmResolution:
    """语音 _build_llm 解析链路（生产确认项）：默认行走 routing voice；
    非默认别名行走助手端点；model_override 覆盖模型名。"""

    def _fake_session(self, voice_assistant=None):
        from app.core.config import get_config
        return SimpleNamespace(config=get_config(), _voice_assistant=voice_assistant)

    def test_default_voice_assistant_uses_voice_routing(self):
        from app.services.voice_service import VoiceDuplexSession
        fake = self._fake_session(
            voice_assistant=SimpleNamespace(
                model_alias="deepseek",  # 默认别名=未显式选择
                custom_model_name=None, provider_type="deepseek", use_custom_model=False,
            )
        )
        svc, model = VoiceDuplexSession._build_llm(fake)
        ep = getattr(svc, "endpoint", None)
        assert ep is not None
        assert ep.alias == "main"  # routing voice → main（voice provider 未配置时）
        assert svc.custom_model_name == model

    def test_explicit_alias_wins(self):
        from app.services.voice_service import VoiceDuplexSession
        fake = self._fake_session(
            voice_assistant=SimpleNamespace(
                model_alias="qwen3.8",  # 非默认别名=显式选择
                custom_model_name=None, provider_type="deepseek", use_custom_model=False,
            )
        )
        svc, model = VoiceDuplexSession._build_llm(fake)
        ep = getattr(svc, "endpoint", None)
        assert ep is not None and ep.alias == "qwen3.8"

    def test_explicit_subpurpose_routes_to_pool_member(self):
        """W-B 池化（2026-08-31）：model_override 机制由 voice.* 子 purpose
        路由取代——显式 [routing."voice.duplex"] = 专门设置例外，走池成员。"""
        from app.services.voice_service import VoiceDuplexSession
        from app.model_gateway.registry import ModelRegistry
        conf = {
            "api": {"base_url": "https://api.deepseek.com/v1", "api_key": "sk", "model_name": "deepseek-chat"},
            "endpoints": {
                "main": {"kind": "llm", "base_url": "https://api.deepseek.com/v1", "api_key": "sk",
                         "model_name": "deepseek-chat"},
                "mimo": {"kind": "llm", "base_url": "http://mimo/v1", "api_key": "sk-m",
                         "model_name": "some-classifier-model"},
            },
            "routing": {"voice.duplex": {"alias": "mimo", "is_custom": True}},
        }
        import app.model_gateway.registry as reg_mod
        reg = ModelRegistry.from_sources(conf)
        fake = self._fake_session(voice_assistant=None)
        orig = reg_mod.get_model_registry
        reg_mod.get_model_registry = lambda: reg
        try:
            svc, model = VoiceDuplexSession._build_llm(fake, purpose="voice.duplex")
        finally:
            reg_mod.get_model_registry = orig
        assert svc.custom_model_name == "some-classifier-model"
        assert model == "some-classifier-model"

    def test_subpurpose_inherits_without_explicit_routing(self):
        from app.services.voice_service import VoiceDuplexSession
        from app.model_gateway.registry import ModelRegistry
        conf = {
            "api": {"base_url": "https://api.deepseek.com/v1", "api_key": "sk", "model_name": "deepseek-chat"},
            "endpoints": {"main": {"kind": "llm", "base_url": "https://api.deepseek.com/v1",
                                   "api_key": "sk", "model_name": "deepseek-chat"}},
        }
        import app.model_gateway.registry as reg_mod
        reg = ModelRegistry.from_sources(conf)
        fake = self._fake_session(voice_assistant=None)
        orig = reg_mod.get_model_registry
        reg_mod.get_model_registry = lambda: reg
        try:
            svc, model = VoiceDuplexSession._build_llm(fake, purpose="voice.duplex")
        finally:
            reg_mod.get_model_registry = orig
        assert model == "deepseek-chat", "无显式子路由 → 继承主语音端点"
