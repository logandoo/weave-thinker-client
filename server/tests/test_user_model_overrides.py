"""TDD — 用户级模型供应商覆盖核心（app/model_gateway/user_overrides.py + registry 出口）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_user_model_overrides.py -v
"""
from types import SimpleNamespace

import pytest

from app.model_gateway import user_overrides as uo
from app.model_gateway.registry import ModelRegistry
from app.model_gateway.schemas import KIND_EMBEDDING, KIND_LLM, ModelEndpoint


def _llm_ep(**kw) -> ModelEndpoint:
    base = dict(
        alias="main", kind=KIND_LLM,
        base_url="https://sys.example/v1", api_key="sys-key", model_name="sys-model",
        is_custom=False, params={"temperature": 0.3, "top_p": 0.9},
    )
    base.update(kw)
    return ModelEndpoint(**base)


@pytest.fixture(autouse=True)
def _clean_ctx():
    uo.reset_active_user_overrides()
    yield
    uo.reset_active_user_overrides()


class TestApplyUserOverride:
    def test_no_active_context_returns_same(self):
        ep = _llm_ep()
        assert uo.apply_user_override(ep) is ep

    def test_empty_context_returns_same(self):
        uo.set_active_user_overrides({})
        ep = _llm_ep()
        assert uo.apply_user_override(ep) is ep

    def test_disabled_override_ignored(self):
        uo.set_active_user_overrides({"llm": {"enabled": False, "base_url": "https://u.example/v1"}})
        assert uo.apply_user_override(_llm_ep()).base_url == "https://sys.example/v1"

    def test_all_empty_override_ignored(self):
        uo.set_active_user_overrides({"llm": {"enabled": True, "base_url": "", "model_name": "", "params": {}}})
        ep = uo.apply_user_override(_llm_ep())
        assert ep.base_url == "https://sys.example/v1" and ep.model_name == "sys-model"

    def test_custom_url_with_key(self):
        uo.set_active_user_overrides({"llm": {
            "enabled": True, "base_url": "https://u.example/v1", "api_key": "u-key",
            "model_name": "u-model", "params": {"temperature": 0.7},
        }})
        ep = uo.apply_user_override(_llm_ep())
        assert ep.base_url == "https://u.example/v1"
        assert ep.api_key == "u-key"
        assert ep.model_name == "u-model"
        assert ep.is_custom is True
        assert ep.params == {"temperature": 0.7, "top_p": 0.9}

    def test_custom_url_without_key_sends_empty_never_system_key(self):
        uo.set_active_user_overrides({"llm": {
            "enabled": True, "base_url": "https://u.example/v1", "api_key": "",
        }})
        ep = uo.apply_user_override(_llm_ep())
        assert ep.api_key == ""  # 消费层 → "no-key" 哨兵；绝不回落 sys-key

    def test_model_params_only_keeps_system_url_and_key(self):
        uo.set_active_user_overrides({"llm": {
            "enabled": True, "base_url": "", "api_key": "ignored",
            "model_name": "u-model", "params": {"temperature": 0.7},
        }})
        ep = uo.apply_user_override(_llm_ep())
        assert ep.base_url == "https://sys.example/v1"
        assert ep.api_key == "sys-key"
        assert ep.model_name == "u-model"
        assert ep.params["temperature"] == 0.7

    def test_params_null_values_removed_not_override(self):
        uo.set_active_user_overrides({"llm": {
            "enabled": True, "params": {"temperature": 0.7, "top_p": None},
        }})
        ep = uo.apply_user_override(_llm_ep())
        assert ep.params == {"temperature": 0.7, "top_p": 0.9}

    def test_kind_isolation(self):
        uo.set_active_user_overrides({"llm": {"enabled": True, "base_url": "https://u.example/v1"}})
        emb = ModelEndpoint(alias="embedding", kind=KIND_EMBEDDING, base_url="https://sys.example/v1")
        assert uo.apply_user_override(emb) is emb

    def test_vlm_key_used_for_vlm_alias_only(self):
        vlm = _llm_ep(alias="vlm", is_custom=True)
        uo.set_active_user_overrides({"vlm": {"enabled": True, "base_url": "https://v.example/v1"}})
        assert uo.apply_user_override(vlm).base_url == "https://v.example/v1"
        # llm 覆盖不得误伤 vlm（两者是不同模型类型）
        uo.reset_active_user_overrides()
        uo.set_active_user_overrides({"llm": {"enabled": True, "base_url": "https://u.example/v1"}})
        assert uo.apply_user_override(vlm).base_url == "https://sys.example/v1"

    def test_llm_provider_key_applies_to_its_alias_only(self):
        """逐供应商覆写（2026-09-15）：llm:<alias> 只命中该供应商端点。"""
        uo.set_active_user_overrides({"llm:qwen3.8": {
            "enabled": True, "base_url": "https://q.example/v1", "model_name": "q-model",
        }})
        q = _llm_ep(alias="qwen3.8", is_custom=True)
        assert uo.apply_user_override(q).base_url == "https://q.example/v1"
        # 其他 LLM 别名不受影响
        assert uo.apply_user_override(_llm_ep(alias="deepseek", is_custom=True)).base_url == "https://sys.example/v1"
        assert uo.apply_user_override(_llm_ep(alias="main")).base_url == "https://sys.example/v1"

    def test_provider_specific_wins_over_legacy_llm(self):
        """provider 专属行优先于 legacy 全局 llm 行。"""
        uo.set_active_user_overrides({
            "llm": {"enabled": True, "params": {"temperature": 0.1}},
            "llm:qwen3.8": {"enabled": True, "params": {"temperature": 0.9}},
        })
        q = uo.apply_user_override(_llm_ep(alias="qwen3.8", is_custom=True))
        assert q.params["temperature"] == 0.9
        # 无专属行的别名走 legacy 回落
        m = uo.apply_user_override(_llm_ep(alias="deepseek", is_custom=True))
        assert m.params["temperature"] == 0.1

    def test_disabled_provider_row_blocks_legacy_fallback(self):
        """专属行存在但 enabled=false → 该供应商无覆盖（不回落到 legacy 全局行）。"""
        uo.set_active_user_overrides({
            "llm": {"enabled": True, "params": {"temperature": 0.1}},
            "llm:qwen3.8": {"enabled": False, "params": {"temperature": 0.9}},
        })
        q = uo.apply_user_override(_llm_ep(alias="qwen3.8", is_custom=True))
        assert q.params["temperature"] == 0.3  # 系统原值，不被任何覆盖改写
        m = uo.apply_user_override(_llm_ep(alias="deepseek", is_custom=True))
        assert m.params["temperature"] == 0.1  # 其他别名 legacy 回落照常

    def test_vlm_isolated_from_provider_keys(self):
        """llm:<alias> 不得误伤 vlm 端点（vlm 是 purpose 端点）。"""
        vlm = _llm_ep(alias="vlm", is_custom=True)
        uo.set_active_user_overrides({"llm:vlm": {"enabled": True, "base_url": "https://x/v1"}})
        assert uo.apply_user_override(vlm).base_url == "https://sys.example/v1"
        uo.set_active_user_overrides({"llm": {"enabled": True, "base_url": "https://u/v1"}})
        assert uo.apply_user_override(vlm).base_url == "https://sys.example/v1"

    def test_user_params_marked_in_extra(self):
        uo.set_active_user_overrides({"llm": {"enabled": True, "params": {"temperature": 0.42}}})
        ep = uo.apply_user_override(_llm_ep())
        assert ep.extra.get("user_params") == {"temperature": 0.42}

    def test_asr_custom_url_forces_general_and_clears_dashscope_keys(self):
        """A4.9 Critical 回归：自定义 ASR URL 必须切 general 引擎并清空
        dashscope 专属键（否则 dashscope 引擎忽略 URL 且旁路取系统 Key）。"""
        from app.model_gateway.schemas import KIND_ASR
        asr_ep = ModelEndpoint(
            alias="asr", kind=KIND_ASR, base_url="https://sys-asr/v1", api_key="sys-asr-key",
            model_name="sys-asr-model", provider_type="dashscope", is_custom=True,
            extra={"dashscope_api_key": "sys-dash-key", "dashscope_model": "fun-asr",
                   "dashscope_vocabulary_url": "https://dashscope.example/vocab",
                   "is_dashscope": True},
        )
        uo.set_active_user_overrides({"asr": {
            "enabled": True, "base_url": "https://user-asr/v1", "api_key": "",
            "model_name": "user-asr-model",
        }})
        ep = uo.apply_user_override(asr_ep)
        assert ep.base_url == "https://user-asr/v1"
        assert ep.provider_type == "general"
        assert ep.api_key == ""
        assert ep.extra["dashscope_api_key"] == ""
        assert ep.extra["dashscope_vocabulary_url"] == ""
        assert ep.extra["is_dashscope"] is False

    def test_asr_model_only_override_keeps_engine(self):
        from app.model_gateway.schemas import KIND_ASR
        asr_ep = ModelEndpoint(
            alias="asr", kind=KIND_ASR, base_url="https://sys-asr/v1", api_key="sys-asr-key",
            model_name="sys-asr-model", provider_type="dashscope", is_custom=True,
            extra={"dashscope_api_key": "sys-dash-key"},
        )
        uo.set_active_user_overrides({"asr": {"enabled": True, "model_name": "u-asr"}})
        ep = uo.apply_user_override(asr_ep)
        assert ep.provider_type == "dashscope"
        assert ep.extra["dashscope_api_key"] == "sys-dash-key"


class TestCacheKeyRegression:
    """A4.9 Critical 回归：params-only 覆盖与系统端点不得共享 LLMService 缓存实例。"""

    def test_params_only_override_not_cached_as_system(self):
        from app.services.agent_service import AgentService
        svc = AgentService()
        uo.reset_active_user_overrides()
        system_llm = svc.create_llm_service(None)
        uo.set_active_user_overrides({"llm": {"enabled": True, "params": {"temperature": 0.42}}})
        user_llm = svc.create_llm_service(None)
        assert user_llm is not system_llm
        assert (user_llm.endpoint.extra or {}).get("user_params") == {"temperature": 0.42}
        # 系统缓存实例不得被用户参数污染
        assert not (system_llm.endpoint.extra or {}).get("user_params")
        uo.reset_active_user_overrides()

    def test_system_built_after_user_not_contaminated(self):
        """A4.9 复审 Minor 6：双向覆盖——用户实例先建时系统实例不得复用其参数。"""
        from app.services.agent_service import AgentService
        svc = AgentService()
        uo.set_active_user_overrides({"llm": {"enabled": True, "params": {"temperature": 0.42}}})
        user_llm = svc.create_llm_service(None)
        uo.reset_active_user_overrides()
        system_llm = svc.create_llm_service(None)
        assert system_llm is not user_llm
        assert not (system_llm.endpoint.extra or {}).get("user_params")


class TestRerankAuthKey:
    """A4.9 Critical 回归：rerank 空 Key 绝不回落系统 Key。"""

    def test_override_empty_key_stays_empty(self):
        from types import SimpleNamespace
        from app.services.memory_retrieval_service import _rerank_auth_key
        ep = SimpleNamespace(api_key="")
        assert _rerank_auth_key(ep, "sys-rerank-key") == ""

    def test_no_endpoint_falls_back_to_config(self):
        from app.services.memory_retrieval_service import _rerank_auth_key
        assert _rerank_auth_key(None, "sys-rerank-key") == "sys-rerank-key"

    def test_endpoint_key_used(self):
        from types import SimpleNamespace
        from app.services.memory_retrieval_service import _rerank_auth_key
        ep = SimpleNamespace(api_key="user-key")
        assert _rerank_auth_key(ep, "sys-rerank-key") == "user-key"


class TestUserParamsPrecedence:
    """用户显式参数必须压过 per-call kwargs（主回复硬编码 temperature=0.7 等）。"""

    def _svc(self, ep):
        from app.services.llm_service import LLMService
        return LLMService(custom_api_url="http://127.0.0.1:9/v1", custom_api_key="k",
                          custom_model_name="m", endpoint=ep)

    def test_user_params_override_call_kwargs(self):
        uo.set_active_user_overrides({"llm": {"enabled": True,
                                              "params": {"temperature": 0.42, "max_tokens": 64}}})
        ep = uo.apply_user_override(_llm_ep(is_custom=True))
        svc = self._svc(ep)
        params = svc._build_params([{"role": "user", "content": "hi"}], temperature=0.7)
        assert params["temperature"] == 0.42
        assert params["max_tokens"] == 64

    def test_no_override_keeps_call_kwargs(self):
        svc = self._svc(_llm_ep(is_custom=True))
        params = svc._build_params([{"role": "user", "content": "hi"}], temperature=0.7)
        assert params["temperature"] == 0.7


MINIMAL_CONF = {
    "api": {"base_url": "https://sys.example/v1", "api_key": "sys-key", "model_name": "sys-model"},
    "defaults": {"temperature": 0.3},
    "vlm": {"base_url": "https://vlm.example/v1", "api_key": "vlm-key", "model_name": "vlm-model"},
}


class TestRegistryIntegration:
    def test_get_applies_override(self):
        reg = ModelRegistry.from_sources(MINIMAL_CONF)
        uo.set_active_user_overrides({"llm": {"enabled": True, "base_url": "https://u.example/v1", "model_name": "u-model"}})
        ep = reg.get("main")
        assert ep.base_url == "https://u.example/v1" and ep.model_name == "u-model"

    def test_resolve_applies_override(self):
        reg = ModelRegistry.from_sources(MINIMAL_CONF)
        uo.set_active_user_overrides({"llm": {"enabled": True, "base_url": "https://u.example/v1"}})
        assert reg.resolve("judge").base_url == "https://u.example/v1"

    def test_endpoint_for_assistant_applies_override(self):
        reg = ModelRegistry.from_sources(MINIMAL_CONF)
        uo.set_active_user_overrides({"llm": {"enabled": True, "base_url": "https://u.example/v1"}})
        assistant = SimpleNamespace(model_alias="deepseek", provider_type="deepseek",
                                    use_custom_model=False, custom_api_url=None,
                                    custom_api_key=None, custom_model_name=None)
        assert reg.endpoint_for_assistant(assistant).base_url == "https://u.example/v1"

    def test_vlm_purpose_uses_vlm_override_not_llm(self):
        reg = ModelRegistry.from_sources(MINIMAL_CONF)
        uo.set_active_user_overrides({"llm": {"enabled": True, "base_url": "https://u.example/v1"}})
        assert reg.resolve("vlm").base_url == "https://vlm.example/v1"
        uo.set_active_user_overrides({"vlm": {"enabled": True, "base_url": "https://v2.example/v1"}})
        assert reg.resolve("vlm").base_url == "https://v2.example/v1"

    def test_reset_restores_system_endpoint(self):
        reg = ModelRegistry.from_sources(MINIMAL_CONF)
        uo.set_active_user_overrides({"llm": {"enabled": True, "base_url": "https://u.example/v1"}})
        assert reg.get("main").base_url == "https://u.example/v1"
        uo.reset_active_user_overrides()
        assert reg.get("main").base_url == "https://sys.example/v1"

    def test_llm_cache_capacity_bound(self):
        """A4.9 Minor：_llm_cache 容量有上限（≤64，FIFO 淘汰）。"""
        from app.services.agent_service import AgentService
        svc = AgentService()
        AgentService._llm_cache.clear()
        try:
            for i in range(100):
                AgentService._llm_cache[(f"fake-endpoint-{i}",)] = object()
            svc.create_llm_service(None)
            assert len(AgentService._llm_cache) <= 64
        finally:
            AgentService._llm_cache.clear()


class TestContextIsolation:
    """R4（2026-09-13 审计 F-G）：`deps.get_user_from_token` 的请求级激活
    不做 finally reset（reset 会先于 endpoint 业务执行）。隔离不变量由
    asyncio 任务上下文副本保证——本测试锁死「并发任务互不可见」。"""

    def test_overrides_are_task_local(self):
        import asyncio

        seen = {}

        async def request_a():
            uo.set_active_user_overrides(
                {"llm": {"enabled": True, "base_url": "https://a.example/v1"}})
            await asyncio.sleep(0)
            seen["a"] = uo.get_active_user_overrides()

        async def request_b():
            await asyncio.sleep(0)
            seen["b"] = uo.get_active_user_overrides()

        async def main():
            # gather 在调用时创建两个 task：各自复制创建时的（干净）上下文。
            await asyncio.gather(
                asyncio.create_task(request_a()),
                asyncio.create_task(request_b()),
            )

        asyncio.run(main())
        assert seen["a"] and seen["a"]["llm"]["base_url"] == "https://a.example/v1"
        assert seen["b"] is None, "任务 B 看到了任务 A 的覆盖——任务隔离被破坏"
