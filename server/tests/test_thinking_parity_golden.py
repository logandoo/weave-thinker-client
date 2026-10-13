"""特征化金测试（characterization golden）：现行 thinking wire 行为全矩阵。

这些断言刻画**重构前**的确切行为；profile 化重构后必须逐字保持。
Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_thinking_parity_golden.py -v
"""
import pytest

from app.services.provider_router import build_thinking_extra_body


class TestBuildThinkingExtraBodyGolden:
    def test_deepseek_enable_no_effort(self):
        assert build_thinking_extra_body("deepseek", True) == {"thinking": {"type": "enabled"}}

    def test_deepseek_enable_with_effort_and_budget(self):
        assert build_thinking_extra_body("deepseek", True, "high", thinking_budget=8192) == {
            "thinking": {"type": "enabled"}, "reasoning_effort": "high", "thinking_budget": 8192,
        }

    def test_deepseek_invalid_effort_dropped(self):
        assert build_thinking_extra_body("deepseek", True, "xhigh") == {"thinking": {"type": "enabled"}}

    def test_deepseek_disable(self):
        assert build_thinking_extra_body("deepseek", False) == {"thinking": {"type": "disabled"}}

    def test_zhipu_enable_no_effort_on_wire(self):
        assert build_thinking_extra_body("zhipu", True, "high") == {"thinking": {"type": "enabled"}}

    def test_qwen_enable(self):
        assert build_thinking_extra_body("qwen", True) == {"enable_thinking": True}

    def test_qwen38_enable_full(self):
        assert build_thinking_extra_body(
            "qwen3.8_vllm", True, "xhigh", thinking_budget=32768, preserve_thinking=True
        ) == {"chat_template_kwargs": {
            "enable_thinking": True, "reasoning_effort": "xhigh",
            "preserve_thinking": True, "thinking_budget": 32768,
        }}

    def test_qwen38_enable_preserve_false(self):
        assert build_thinking_extra_body("qwen3.8_vllm", True, "medium", preserve_thinking=False) == {
            "chat_template_kwargs": {
                "enable_thinking": True, "reasoning_effort": "medium", "preserve_thinking": False,
            },
        }

    def test_qwen38_disable(self):
        assert build_thinking_extra_body("qwen3.8_vllm", False) == {
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def test_qwen38_budget_not_added_when_disabled(self):
        assert build_thinking_extra_body("qwen3.8_vllm", False, thinking_budget=100) == {
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def test_qwen38_disable_keeps_preserve(self):
        # A4.9 wave-7 复审 Critical：旧代码 preserve_thinking 不门控 enable——
        # 关思考也携带（grace/retry/thinking-off 路径依赖 vLLM 模板保真）
        assert build_thinking_extra_body("qwen3.8_vllm", False, preserve_thinking=True) == {
            "chat_template_kwargs": {"enable_thinking": False, "preserve_thinking": True},
        }
        assert build_thinking_extra_body("qwen3.8_vllm", False, preserve_thinking=False) == {
            "chat_template_kwargs": {"enable_thinking": False, "preserve_thinking": False},
        }

    def test_unknown_provider_generic(self):
        assert build_thinking_extra_body("glm5", True) == {"thinking": {"type": "enabled"}}

    def test_mimo_generic_shape(self):
        assert build_thinking_extra_body("mimo", False) == {"thinking": {"type": "disabled"}}

    def test_custom_with_custom_extra_body_merge(self):
        body = build_thinking_extra_body(
            "custom", True, custom_extra_body='{"foo": 1, "thinking": {"type": "disabled"}}'
        )
        # thinking_params 覆盖 custom_extra_body 同名字段
        assert body == {"foo": 1, "thinking": {"type": "enabled"}}

    def test_custom_invalid_json_extra_body(self):
        assert build_thinking_extra_body("custom", True, custom_extra_body="{bad json") == {
            "thinking": {"type": "enabled"},
        }


class TestPreserveThinkingGolden:
    def test_membership(self):
        from app.services.llm_service import PRESERVE_THINKING_PROVIDERS
        for pt in ("qwen3.8_vllm", "deepseek", "mimo"):
            assert pt in PRESERVE_THINKING_PROVIDERS
        for pt in ("glm5", "qwen", "zhipu", "custom", ""):
            assert pt not in PRESERVE_THINKING_PROVIDERS


class TestCustomNormalizationGolden:
    """llm_service._build_params 的 custom provider extra_body 翻译（现行行为）。"""

    def _svc(self, base_url, model_name):
        from app.services.llm_service import LLMService
        return LLMService(custom_api_url=base_url, custom_api_key="k", custom_model_name=model_name)

    def _params(self, svc, extra_body):
        return svc._build_params([{"role": "user", "content": "hi"}], extra_body=extra_body)

    def test_dashscope_enable_thinking_stays_toplevel(self):
        svc = self._svc("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3-max")
        p = self._params(svc, {"enable_thinking": False})
        assert p["extra_body"] == {"enable_thinking": False}

    def test_nondashscope_enable_thinking_moves_to_ctk(self):
        svc = self._svc("http://vllm.local:8000/v1", "some-model")
        p = self._params(svc, {"enable_thinking": False})
        assert p["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}

    def test_qwen_model_thinking_type_translated(self):
        svc = self._svc("http://vllm.local:8000/v1", "Qwen3.8-27B")
        p = self._params(svc, {"thinking": {"type": "enabled"}})
        assert p["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}

    def test_qwen_model_thinking_budget_translated(self):
        svc = self._svc("http://vllm.local:8000/v1", "qwen3.8_27b")
        p = self._params(svc, {"thinking_budget": 4096})
        assert p["extra_body"] == {"chat_template_kwargs": {"thinking_budget": 4096}}

    def test_qwen_model_enable_thinking_and_budget_merge(self):
        svc = self._svc("http://vllm.local:8000/v1", "qwen-x")
        p = self._params(svc, {"enable_thinking": True, "thinking_budget": 1024, "foo": 1})
        assert p["extra_body"] == {
            "chat_template_kwargs": {"enable_thinking": True, "thinking_budget": 1024},
            "foo": 1,
        }

    def test_nonqwen_thinking_type_passthrough(self):
        svc = self._svc("http://vllm.local:8000/v1", "llama-3")
        p = self._params(svc, {"thinking": {"type": "enabled"}})
        assert p["extra_body"] == {"thinking": {"type": "enabled"}}

    def test_sdk_extra_merged(self):
        svc = self._svc("http://vllm.local:8000/v1", "some-model")
        p = svc._build_params([{"role": "user", "content": "hi"}], extra_body={"a": 1}, top_k=20)
        assert p["extra_body"] == {"a": 1, "top_k": 20}


class TestThinkingOffGolden:
    """llm_service._thinking_off_extra_body（重试关思考，现行行为）。"""

    def _off(self, base_url, model_name):
        from app.services.llm_service import LLMService
        svc = LLMService(custom_api_url=base_url, custom_api_key="k", custom_model_name=model_name)
        return svc._thinking_off_extra_body()

    def test_dashscope_off(self):
        assert self._off("https://dashscope.aliyuncs.com/v1", "qwen3-max") == {"enable_thinking": False}

    def test_qwen_vllm_off(self):
        assert self._off("http://vllm.local:8000/v1", "qwen3.8_27b") == {
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def test_generic_off(self):
        assert self._off("http://x.local/v1", "llama-3") == {"thinking": {"type": "disabled"}}

    def test_bare_off(self):
        from app.services.llm_service import LLMService
        svc = LLMService()
        # 裸构造（config 主端点 deepseek）：走 generic
        assert svc._thinking_off_extra_body() == {"thinking": {"type": "disabled"}}


class TestVoiceThinkingOffGolden:
    """voice_service._thinking_off_body 的旧口径（仅 "dashscope"，A4.9 R2 Minor2）。"""

    def _off(self, base_url):
        from types import SimpleNamespace
        from app.services.voice_service import VoiceDuplexSession
        from app.core.config import get_config
        svc = SimpleNamespace(client=SimpleNamespace(base_url=base_url))
        fake = SimpleNamespace(config=get_config(), _voice_assistant=None)
        return VoiceDuplexSession._thinking_off_body(fake, svc)

    def test_dashscope_goes_qwen_profile(self):
        assert self._off("https://dashscope.aliyuncs.com/v1") == {"enable_thinking": False}

    def test_aliyuncs_without_dashscope_goes_generic(self):
        # 旧行为：仅匹配 "dashscope" 字面量 → PAI-EAS 等 aliyuncs 地址走 generic
        assert self._off("https://pai-eas.aliyuncs.com/v1") == {"thinking": {"type": "disabled"}}

    def test_deepseek_generic(self):
        assert self._off("https://api.deepseek.com/v1") == {"thinking": {"type": "disabled"}}


class TestSamplingPresetsGolden:
    def test_qwen38_preset_keys(self):
        # wave-7：常量从 provider_router 迁至 thinking profile（值不变=行为契约）
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("qwen3.8_vllm")
        thinking_defaults = p.sampling_defaults(True)
        non_thinking_defaults = p.sampling_defaults(False)
        # 全量模型卡值钉住（A4.9 wave-7 复审 Minor1）
        assert thinking_defaults == {
            "temperature": 1.0, "top_p": 0.95, "top_k": 20,
            "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.0,
        }
        assert non_thinking_defaults == {
            "temperature": 0.7, "top_p": 0.80, "top_k": 20,
            "min_p": 0.0, "presence_penalty": 1.5, "repetition_penalty": 1.0,
        }
