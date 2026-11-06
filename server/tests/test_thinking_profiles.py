"""TDD tests for model_gateway.profiles（同协议厂商 thinking 适配器，LiteLLM 范式）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_thinking_profiles.py -v
"""
import pytest


class TestProfileRegistry:
    def test_known_provider_types(self):
        from app.model_gateway.profiles import get_thinking_profile
        assert get_thinking_profile("deepseek").name == "deepseek"
        assert get_thinking_profile("qwen3.8_vllm").name == "qwen3.8_vllm"
        assert get_thinking_profile("qwen").name == "qwen"
        assert get_thinking_profile("zhipu").name == "zhipu"
        assert get_thinking_profile("mimo").name == "mimo"

    def test_unknown_falls_back_generic(self):
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("glm5")
        assert p.enable() == {"thinking": {"type": "enabled"}}
        assert p.disable() == {"thinking": {"type": "disabled"}}

    def test_custom_maps_generic(self):
        from app.model_gateway.profiles import get_thinking_profile
        assert get_thinking_profile("custom").name == "generic"
        assert get_thinking_profile("").name == "generic"


class TestProfileWireShapes:
    def test_deepseek_enable_effort_only(self):
        """2026-10-18: DeepSeek documents effort only; thinking_budget is not
        a wire field (harness-aligned). Passing budget must not emit it."""
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("deepseek")
        assert p.enable(effort="max", budget=1024) == {
            "thinking": {"type": "enabled"}, "reasoning_effort": "max",
        }
        assert p.efforts == ("low", "high", "max")

    def test_deepseek_sanitize_thinking_params(self):
        """Thinking mode ignores temperature/penalties; top_p clamps to 0.95."""
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("deepseek")
        out = p.sanitize_request({
            "temperature": 0.7, "top_p": 0.9,
            "presence_penalty": 1.5, "frequency_penalty": 0.0,
            "extra_body": {"thinking": {"type": "enabled"}, "thinking_budget": 512},
        }, thinking=True)
        assert "temperature" not in out
        assert "presence_penalty" not in out
        assert "frequency_penalty" not in out
        assert out["top_p"] == 0.95
        assert "thinking_budget" not in out["extra_body"]
        # Non-thinking: sampling params stay (they are effective there).
        out2 = p.sanitize_request({"temperature": 0.7, "top_p": 0.9}, thinking=False)
        assert out2["temperature"] == 0.7 and out2["top_p"] == 0.9

    def test_qwen38_enable_ctk(self):
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("qwen3.8_vllm")
        assert p.enable(effort="xhigh", budget=100, preserve=True) == {
            "chat_template_kwargs": {
                "enable_thinking": True, "reasoning_effort": "xhigh",
                "preserve_thinking": True, "thinking_budget": 100,
            },
        }
        assert p.efforts == ("xhigh", "medium", "low")

    def test_qwen38_merge_extra_params_nested(self):
        """_thinking_extra_body 的嵌套合并逻辑迁入 profile（params 全量入 ctk）。"""
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("qwen3.8_vllm")
        body = p.enable(effort="low")
        merged = p.merge_extra_params(body, {"thinking_budget": 2048, "foo": 1})
        assert merged == {
            "chat_template_kwargs": {
                "enable_thinking": True, "reasoning_effort": "low",
                "thinking_budget": 2048, "foo": 1,
            },
        }

    def test_generic_merge_extra_params_toplevel(self):
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("deepseek")
        merged = p.merge_extra_params(p.enable(), {"x": 1})
        assert merged == {"thinking": {"type": "enabled"}, "x": 1}

    def test_merge_nested_whenever_body_has_ctk(self):
        """A4.9 wave-7 R2 Minor1：合并语义与旧 agent_loop 内联一致——
        任何 profile，body 有 ctk 即嵌套合并（与 provider 无关）。"""
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("deepseek")  # 非 qwen38 profile 也必须嵌套
        body = {"chat_template_kwargs": {"enable_thinking": True}, "thinking": {"type": "enabled"}}
        merged = p.merge_extra_params(body, {"thinking_budget": 100, "foo": 1})
        assert merged == {
            "chat_template_kwargs": {"enable_thinking": True, "thinking_budget": 100, "foo": 1},
            "thinking": {"type": "enabled"},
        }


class TestDoubaoProfile:
    """Doubao（火山方舟 Ark）：thinking{type} 开/关 + 顶层 reasoning_effort；
    无 thinking_budget 变量（所有 wire 路径剥除）。实测依据：Ark 真实 API
    （doubao-seed-2-0-pro-260215）：enabled→reasoning_content；disabled→无；
    reasoning_effort low/high 生效；minimal=0 reasoning tokens ≡ 关闭思考。
    2026-10-04 用户裁定：minimal 与关闭重复 → 档位移除（菜单与 wire 均不出现），
    遗留 "minimal" 值兜底映射 disable 保语义保真。"""

    def test_registered(self):
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("doubao")
        assert p.name == "doubao"
        assert p.efforts == ("high", "medium", "low")

    def test_enable_with_effort(self):
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("doubao")
        assert p.enable(effort="high") == {
            "thinking": {"type": "enabled"}, "reasoning_effort": "high",
        }
        assert p.enable(effort="medium") == {
            "thinking": {"type": "enabled"}, "reasoning_effort": "medium",
        }

    def test_legacy_minimal_maps_to_disable(self):
        """遗留 "minimal"（档位已移除）≡ 关闭思考：映射 thinking{disabled}，
        不上 reasoning_effort，绝不静默变成默认深度思考。大小写/空白容错。"""
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("doubao")
        assert p.enable(effort="minimal") == {"thinking": {"type": "disabled"}}
        assert p.enable(effort="minimal", budget=4096) == {"thinking": {"type": "disabled"}}
        assert p.enable(effort="Minimal") == {"thinking": {"type": "disabled"}}
        assert p.enable(effort=" MINIMAL ") == {"thinking": {"type": "disabled"}}

    def test_enable_unknown_effort_omitted(self):
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("doubao")
        assert p.enable(effort="xhigh") == {"thinking": {"type": "enabled"}}
        assert p.enable() == {"thinking": {"type": "enabled"}}

    def test_enable_strips_budget(self):
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("doubao")
        assert p.enable(effort="low", budget=32768) == {
            "thinking": {"type": "enabled"}, "reasoning_effort": "low",
        }

    def test_disable_shape(self):
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("doubao")
        assert p.disable() == {"thinking": {"type": "disabled"}}

    def test_merge_extra_params_strips_budget(self):
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("doubao")
        merged = p.merge_extra_params(
            p.enable(effort="high"), {"thinking_budget": 2048, "x": 1})
        assert merged == {
            "thinking": {"type": "enabled"}, "reasoning_effort": "high", "x": 1,
        }

    def test_normalize_user_extra_body_strips_budget(self):
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("doubao")
        assert p.normalize_user_extra_body(
            {"thinking_budget": 5, "thinking": {"type": "enabled"}}) == {
            "thinking": {"type": "enabled"},
        }

    def test_strip_budget_nested_ctk(self):
        """A4.9 R1 Minor-3：嵌套 chat_template_kwargs.thinking_budget 也必须剥除
        （单条 wire 路径防御；空壳 ctk 清掉）。"""
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("doubao")
        assert p.normalize_user_extra_body(
            {"chat_template_kwargs": {"thinking_budget": 5, "x": 1}}) == {
            "chat_template_kwargs": {"x": 1},
        }
        assert p.normalize_user_extra_body(
            {"chat_template_kwargs": {"thinking_budget": 5}}) == {}
        assert p.merge_extra_params(
            {"chat_template_kwargs": {"thinking_budget": 5}}, {"a": 1}) == {
            "chat_template_kwargs": {"a": 1},
        }


class TestProfilePreserveAndSampling:
    def test_preserve_flags(self):
        from app.model_gateway.profiles import get_thinking_profile
        assert get_thinking_profile("deepseek").preserve_thinking is True
        assert get_thinking_profile("qwen3.8_vllm").preserve_thinking is True
        assert get_thinking_profile("mimo").preserve_thinking is True
        assert get_thinking_profile("glm5").preserve_thinking is False
        assert get_thinking_profile("qwen").preserve_thinking is False

    def test_qwen38_sampling_defaults(self):
        from app.model_gateway.profiles import get_thinking_profile
        p = get_thinking_profile("qwen3.8_vllm")
        assert p.sampling_defaults(True)["temperature"] == 1.0
        assert p.sampling_defaults(False)["temperature"] == 0.7
        assert get_thinking_profile("deepseek").sampling_defaults(True) == {}


class TestSniff:
    def test_deepseek_url(self):
        """2026-10-18: legacy/custom endpoints pointing at DeepSeek must get
        the DeepSeek profile (preserve_thinking + param hygiene), not generic."""
        from app.model_gateway.profiles import sniff_thinking_profile
        p = sniff_thinking_profile("https://api.deepseek.com/v1", "deepseek-flash")
        assert p.name == "deepseek"
        assert p.preserve_thinking is True

    def test_dashscope_url(self):
        from app.model_gateway.profiles import sniff_thinking_profile
        p = sniff_thinking_profile("https://dashscope.aliyuncs.com/v1", "qwen3-max")
        assert p.name == "qwen"
        assert p.disable() == {"enable_thinking": False}
        assert p.normalize_user_extra_body({"enable_thinking": False}) == {"enable_thinking": False}

    def test_qwen_model_vllm(self):
        from app.model_gateway.profiles import sniff_thinking_profile
        p = sniff_thinking_profile("http://vllm.local:8000/v1", "qwen3.8_27b")
        assert p.name == "qwen3.8_vllm"
        body = p.normalize_user_extra_body({"enable_thinking": True, "thinking_budget": 512})
        assert body == {"chat_template_kwargs": {"enable_thinking": True, "thinking_budget": 512}}
        assert p.disable() == {"chat_template_kwargs": {"enable_thinking": False}}

    def test_generic_custom(self):
        from app.model_gateway.profiles import sniff_thinking_profile
        p = sniff_thinking_profile("http://x.local/v1", "llama-3")
        assert p.disable() == {"thinking": {"type": "disabled"}}
        # 非 dashscope 非 qwen：enable_thinking 仍按现行行为迁入 ctk
        assert p.normalize_user_extra_body({"enable_thinking": False, "a": 1}) == {
            "chat_template_kwargs": {"enable_thinking": False}, "a": 1,
        }
        assert p.normalize_user_extra_body({"thinking": {"type": "enabled"}}) == {
            "thinking": {"type": "enabled"},
        }


class TestDeepSeekWireBuild:
    """2026-10-18: final DeepSeek request body matches the documented
    thinking-mode semantics (no ignored sampling params, no budget field)."""

    def test_thinking_wire_shape(self):
        from app.model_gateway import factory
        from app.model_gateway.schemas import KIND_LLM, ModelEndpoint
        ep = ModelEndpoint(
            alias="deepseek", kind=KIND_LLM,
            base_url="https://api.deepseek.com/v1", api_key="test-key",
            model_name="deepseek-flash", provider_type="deepseek",
            is_custom=True, params={"top_p": 0.95},
        )
        llm = factory.build_llm_service(ep, preserve_reasoning=True)
        params = llm._build_params(
            [{"role": "user", "content": "hi"}],
            temperature=0.7,
            presence_penalty=1.5,
            frequency_penalty=0.0,
            top_p=0.9,
            extra_body={
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
                "thinking_budget": 999,
            },
        )
        assert params["model"] == "deepseek-flash"
        assert "temperature" not in params
        assert "presence_penalty" not in params
        assert "frequency_penalty" not in params
        assert params["top_p"] == 0.95
        assert "thinking_budget" not in params["extra_body"]
        assert params["extra_body"]["reasoning_effort"] == "high"


class TestFacadeDelegation:
    """provider_router.build_thinking_extra_body 委托 profile 后行为不变（金测试互证）。"""

    def test_facade_matches_profile(self):
        from app.model_gateway.profiles import get_thinking_profile
        from app.services.provider_router import build_thinking_extra_body
        assert build_thinking_extra_body("qwen3.8_vllm", True, "low", thinking_budget=1) == \
            get_thinking_profile("qwen3.8_vllm").enable(effort="low", budget=1)
        assert build_thinking_extra_body("deepseek", False) == \
            get_thinking_profile("deepseek").disable()
