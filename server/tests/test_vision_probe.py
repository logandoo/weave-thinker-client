"""TDD — 视觉能力探针（model_gateway/vision_probe.py）+ vision_interpret 接线。

需求（2026-09-15 首版）：视觉模型（vlm）未单独配置时，视觉任务先对当前主模型做
一次行为探针（随机 token + 色块图）；有视觉 → 主模型充当视觉模型；无视觉 →
明确失败。探针结果进程内缓存。

需求（2026-11-01 反转，用户明确要求）：**所有视觉能力优先使用当前调用的主模型**
——先探针，探针不通过才调用系统配置的 vlm 模型。旧语义（「vlm 已配置则直接
使用、不探针」）曾让过期的 vlm 配置压过有视觉的主模型（60 事故：mimo-v2.5 被
上游下线 → vision_interpret 400）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_vision_probe.py -v
"""
import asyncio
import base64
import io
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.model_gateway.schemas import KIND_LLM, ModelEndpoint

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _run(coro):
    return asyncio.run(coro)


def _ep(alias="main", **kw):
    base = dict(alias=alias, kind=KIND_LLM, base_url="http://m.local/v1",
                api_key="", model_name="m-1", is_custom=True)
    base.update(kw)
    return ModelEndpoint(**base)


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    from app.model_gateway import vision_probe
    vision_probe._CACHE.clear()
    vision_probe._INFLIGHT.clear()
    yield
    vision_probe._CACHE.clear()
    vision_probe._INFLIGHT.clear()


class _FakeLLM:
    def __init__(self, content=None, exc=None, calls=None):
        self.content = content
        self.exc = exc
        self.calls = calls if calls is not None else []

    async def complete_chat_parts(self, messages, **kwargs):
        self.calls.append(messages)
        if self.exc:
            raise self.exc
        return self.content, ""


class TestProbeImage:
    def test_probe_png_is_valid_and_matches_metadata(self):
        from app.model_gateway import vision_probe
        png, token, color = vision_probe.make_probe_png()
        assert isinstance(png, bytes) and png[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(token) == 6 and token.isalnum()
        assert token.isupper() or any(c.isdigit() for c in token)
        assert color in {c[0] for c in vision_probe.PROBE_COLORS}
        # 可解码 + 尺寸固定（探针图契约）
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        assert img.size == vision_probe.PROBE_IMAGE_SIZE

    def test_probe_question_asks_token_and_color(self):
        from app.model_gateway import vision_probe
        q = vision_probe.PROBE_QUESTION
        assert "颜色" in q


class TestTokenMatching:
    """容 1 字符 OCR 误差（vibeweaver mm_probe 同款 ≥5/6 判据）。"""

    def test_exact_match(self):
        from app.model_gateway.vision_probe import _token_matches
        assert _token_matches("answer: k7m2qx green", "K7M2QX") is True

    def test_one_char_off_window_matches(self):
        """llama.cpp 实测 'CBGTTQ' vs token 'GBGTTQ'（前导误读）——旧精确
        子串判定会假阴性，≥5/6 判据必须通过。"""
        from app.model_gateway.vision_probe import _token_matches
        assert _token_matches("cbgttq 绿色", "GBGTTQ") is True

    def test_two_chars_off_rejected(self):
        from app.model_gateway.vision_probe import _token_matches
        assert _token_matches("xbxttq green", "GBGTTQ") is False

    def test_unrelated_answer_rejected(self):
        from app.model_gateway.vision_probe import _token_matches
        assert _token_matches("i cannot see the image", "GBGTTQ") is False
        assert _token_matches("", "GBGTTQ") is False


class TestProbeVision:
    def test_capable_when_answer_matches(self, monkeypatch):
        from app.model_gateway import vision_probe
        fake = _FakeLLM(content="K7M2QX green")
        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", lambda ep, **k: fake)
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))
        assert _run(vision_probe.probe_vision(_ep())) is True

    def test_capable_accepts_chinese_color(self, monkeypatch):
        from app.model_gateway import vision_probe
        fake = _FakeLLM(content="图中字符是 K7M2QX，方块是绿色")
        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", lambda ep, **k: fake)
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))
        assert _run(vision_probe.probe_vision(_ep())) is True

    def test_not_capable_on_api_error(self, monkeypatch):
        from app.model_gateway import vision_probe
        fake = _FakeLLM(exc=RuntimeError("400 unsupported content type"))
        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", lambda ep, **k: fake)
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))
        assert _run(vision_probe.probe_vision(_ep())) is False

    def test_not_capable_on_hallucinated_answer(self, monkeypatch):
        from app.model_gateway import vision_probe
        fake = _FakeLLM(content="I cannot see the image")
        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", lambda ep, **k: fake)
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))
        assert _run(vision_probe.probe_vision(_ep())) is False

    def test_not_capable_on_wrong_token(self, monkeypatch):
        from app.model_gateway import vision_probe
        fake = _FakeLLM(content="ABC123 green")
        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", lambda ep, **k: fake)
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))
        assert _run(vision_probe.probe_vision(_ep())) is False

    def test_not_capable_on_wrong_color(self, monkeypatch):
        from app.model_gateway import vision_probe
        fake = _FakeLLM(content="K7M2QX red")
        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", lambda ep, **k: fake)
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))
        assert _run(vision_probe.probe_vision(_ep())) is False

    def test_result_cached_no_repeat_probe(self, monkeypatch):
        from app.model_gateway import vision_probe
        calls = []
        fake = _FakeLLM(content="K7M2QX green", calls=calls)
        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", lambda ep, **k: fake)
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))
        assert _run(vision_probe.probe_vision(_ep())) is True
        assert _run(vision_probe.probe_vision(_ep())) is True
        assert len(calls) == 1, "同端点第二次探针必须命中缓存"

    def test_transient_error_not_cached(self, monkeypatch):
        """A4.9 R1 Minor-4：超时/连接类瞬时失败不缓存——否则一次上游抖动
        变成 10 分钟确定性「不具备视觉能力」。"""
        from app.model_gateway import vision_probe
        calls = []

        class _Flaky:
            def __init__(self):
                self.fail = True

            async def complete_chat_parts(self, messages, **kwargs):
                calls.append(1)
                if self.fail:
                    raise asyncio.TimeoutError("probe timeout")
                return "K7M2QX green", ""

        flaky = _Flaky()
        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", lambda ep, **k: flaky)
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))
        assert _run(vision_probe.probe_vision(_ep())) is False
        flaky.fail = False
        assert _run(vision_probe.probe_vision(_ep())) is True, "瞬时失败被缓存，重探未发生"
        assert len(calls) == 2

    def test_client_error_cached_as_definitive(self, monkeypatch):
        """4xx（图片不支持/模型拒绝）是确定性「无视觉」→ 缓存，不重复烧钱。"""
        from app.model_gateway import vision_probe
        calls = []

        class _BadRequest(Exception):
            status_code = 400

        class _Reject:
            async def complete_chat_parts(self, messages, **kwargs):
                calls.append(1)
                raise _BadRequest("unsupported content")

        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", lambda ep, **k: _Reject())
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))
        assert _run(vision_probe.probe_vision(_ep())) is False
        assert _run(vision_probe.probe_vision(_ep())) is False
        assert len(calls) == 1, "4xx 确定性负例应命中缓存"

    def test_negative_cache_ttl_shorter_than_positive(self, monkeypatch):
        """负例 TTL 短于正例（容忍思考模型偶发误读导致的假阴性，代价=廉价重探）。"""
        from app.model_gateway import vision_probe
        assert vision_probe._NEGATIVE_CACHE_TTL_SECONDS < vision_probe._CACHE_TTL_SECONDS

        fake = _FakeLLM(content="i cannot see")
        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", lambda ep, **k: fake)
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))
        assert _run(vision_probe.probe_vision(_ep())) is False
        key = vision_probe._cache_key(_ep())
        exp, cap = vision_probe._CACHE[key]
        import time as _t
        assert cap is False
        assert exp - _t.monotonic() <= vision_probe._NEGATIVE_CACHE_TTL_SECONDS + 1

    def test_empty_key_sends_no_key_sentinel(self, monkeypatch):
        from app.model_gateway import vision_probe
        captured = {}

        def _fake_build(ep_, **k):
            captured["api_key"] = ep_.api_key
            return _FakeLLM(content="K7M2QX green")

        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", _fake_build)
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))
        _run(vision_probe.probe_vision(_ep(api_key="")))
        assert captured["api_key"] == "no-key"


class TestResolveVisionEndpoint:
    def _fake_registry(self, vlm_ep, main_ep):
        class _R:
            def resolve(self, purpose):
                return vlm_ep if purpose == "vlm" else main_ep

            def endpoint_for_assistant(self, assistant):
                return main_ep

        return _R()

    def test_main_model_wins_even_when_vlm_configured(self, monkeypatch):
        """2026-11-01 反转：vlm 已配置也**不得**跳过主模型探针。

        事故回归钉（60 conv 9799b1e6）：主模型有视觉 + [endpoints.vlm] 指向
        已下线模型（mimo-v2.5）→ 必须走主模型，不能撞 400。"""
        from app.model_gateway import vision_probe
        vlm = _ep(alias="vlm", base_url="http://vlm.local/v1", model_name="retired-vlm")
        main = _ep(alias="main")
        monkeypatch.setattr("app.model_gateway.registry.get_model_registry",
                            lambda: self._fake_registry(vlm, main))
        probed = {}

        async def _probe(ep):
            probed["ep"] = ep
            return True

        monkeypatch.setattr(vision_probe, "probe_vision", _probe)
        ep, reason = _run(vision_probe.resolve_vision_endpoint())
        assert ep is main and reason == "main_model"
        assert probed["ep"] is main, "主模型必须被探针"

    def test_configured_vlm_used_when_main_probe_fails(self, monkeypatch):
        """主模型探针不通过 → 才回落系统 [endpoints.vlm]。"""
        from app.model_gateway import vision_probe
        vlm = _ep(alias="vlm", base_url="http://vlm.local/v1", model_name="vlm-1")
        main = _ep(alias="main")
        monkeypatch.setattr("app.model_gateway.registry.get_model_registry",
                            lambda: self._fake_registry(vlm, main))

        async def _probe(ep):
            return False

        monkeypatch.setattr(vision_probe, "probe_vision", _probe)
        ep, reason = _run(vision_probe.resolve_vision_endpoint())
        assert ep is vlm and reason == "configured"

    def test_vlm_resolving_to_main_endpoint_is_not_a_fallback(self, monkeypatch):
        """routing 缺 vlm 时 registry.resolve("vlm") 回落主别名——同一端点
        不能当成「已配置视觉模型」再跑一次（否则探针失败后仍返回它）。"""
        from app.model_gateway import vision_probe
        main = _ep(alias="main", base_url="http://m.local/v1", model_name="m-1")
        same = _ep(alias="vlm", base_url="http://m.local/v1", model_name="m-1")
        monkeypatch.setattr("app.model_gateway.registry.get_model_registry",
                            lambda: self._fake_registry(same, main))

        async def _probe(ep):
            return False

        monkeypatch.setattr(vision_probe, "probe_vision", _probe)
        ep, reason = _run(vision_probe.resolve_vision_endpoint())
        assert ep is None and reason == "no_vision"

    def test_probe_cached_across_resolutions(self, monkeypatch):
        """候选 #4：同一端点连续两次解析只发一次探针（缓存不退化）。"""
        from app.model_gateway import vision_probe
        vlm = _ep(alias="vlm", base_url="http://vlm.local/v1", model_name="vlm-1")
        main = _ep(alias="main")
        monkeypatch.setattr("app.model_gateway.registry.get_model_registry",
                            lambda: self._fake_registry(vlm, main))
        calls = []

        fake = _FakeLLM(content="K7M2QX green", calls=calls)
        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", lambda ep_, **k: fake)
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))
        ep1, r1 = _run(vision_probe.resolve_vision_endpoint())
        ep2, r2 = _run(vision_probe.resolve_vision_endpoint())
        assert (ep1, r1) == (ep2, r2) == (main, "main_model")
        assert len(calls) == 1, "第二次解析必须命中探针缓存"

    def test_unconfigured_vlm_probes_main_and_uses_it(self, monkeypatch):
        from app.model_gateway import vision_probe
        empty = _ep(alias="vlm", base_url="", model_name="")
        main = _ep(alias="main")
        monkeypatch.setattr("app.model_gateway.registry.get_model_registry",
                            lambda: self._fake_registry(empty, main))
        probed = {}

        async def _probe(ep):
            probed["ep"] = ep
            return True

        monkeypatch.setattr(vision_probe, "probe_vision", _probe)
        ep, reason = _run(vision_probe.resolve_vision_endpoint())
        assert ep is main and reason == "main_model"
        assert probed["ep"] is main

    def test_unconfigured_vlm_and_no_vision_returns_none(self, monkeypatch):
        from app.model_gateway import vision_probe
        empty = _ep(alias="vlm", base_url="", model_name="")
        main = _ep(alias="main")
        monkeypatch.setattr("app.model_gateway.registry.get_model_registry",
                            lambda: self._fake_registry(empty, main))

        async def _probe(ep):
            return False

        monkeypatch.setattr(vision_probe, "probe_vision", _probe)
        ep, reason = _run(vision_probe.resolve_vision_endpoint())
        assert ep is None and reason == "no_vision"

    def test_main_endpoint_empty_is_no_vision(self, monkeypatch):
        from app.model_gateway import vision_probe
        empty = _ep(alias="vlm", base_url="", model_name="")
        bare = _ep(alias="main", base_url="", model_name="")
        monkeypatch.setattr("app.model_gateway.registry.get_model_registry",
                            lambda: self._fake_registry(empty, bare))
        ep, reason = _run(vision_probe.resolve_vision_endpoint())
        assert ep is None and reason == "no_vision"

    def test_explicit_main_endpoint_takes_priority(self, monkeypatch):
        """A4.9 R1 Minor-5：调用方持有助手端点（如 deathmatch visual_critic
        的 _assistant_llm.endpoint）时优先探它，而非 registry main。"""
        from app.model_gateway import vision_probe
        empty = _ep(alias="vlm", base_url="", model_name="")
        registry_main = _ep(alias="main", model_name="m")
        assistant_ep = _ep(alias="qwen3.8", model_name="q")
        monkeypatch.setattr("app.model_gateway.registry.get_model_registry",
                            lambda: self._fake_registry(empty, registry_main))
        probed = {}

        async def _probe(ep):
            probed["ep"] = ep
            return True

        monkeypatch.setattr(vision_probe, "probe_vision", _probe)
        ep, reason = _run(vision_probe.resolve_vision_endpoint(main_endpoint=assistant_ep))
        assert ep is assistant_ep and reason == "main_model"
        assert probed["ep"] is assistant_ep

    def test_empty_endpoints_main_falls_back_to_routing_main(self, monkeypatch):
        """A4.9 R1-I1：[endpoints.main] 为空但 [routing] main 指向池中别名时，
        必须探那个别名——否则 vlm 会零探针胜出（事故形态）。"""
        from app.model_gateway import vision_probe
        empty_main = _ep(alias="main", base_url="", model_name="")
        routed = _ep(alias="deepseek", base_url="http://ds.local/v1", model_name="ds-1")
        vlm = _ep(alias="vlm", base_url="http://vlm.local/v1", model_name="vlm-1")

        class _R:
            def resolve(self, purpose):
                return routed if purpose == "main" else vlm

            def endpoint_for_assistant(self, assistant):
                return empty_main

        monkeypatch.setattr("app.model_gateway.registry.get_model_registry", lambda: _R())

        async def _probe(ep):
            return True

        monkeypatch.setattr(vision_probe, "probe_vision", _probe)
        ep, reason = _run(vision_probe.resolve_vision_endpoint())
        assert ep is routed and reason == "main_model"

    def test_assistant_resolution_error_falls_back_to_vlm(self, monkeypatch):
        """A4.9 R1-M2：主端点解析抛错不应整体失败——vlm 回落仍可用。"""
        from app.model_gateway import vision_probe
        vlm = _ep(alias="vlm", base_url="http://vlm.local/v1", model_name="vlm-1")
        empty_main = _ep(alias="main", base_url="", model_name="")

        class _R:
            def resolve(self, purpose):
                return vlm if purpose == "vlm" else empty_main

            def endpoint_for_assistant(self, assistant):
                raise RuntimeError("assistant row has unknown provider_type")

        monkeypatch.setattr("app.model_gateway.registry.get_model_registry", lambda: _R())
        ep, reason = _run(vision_probe.resolve_vision_endpoint())
        assert ep is vlm and reason == "configured"

    def test_cache_key_separates_credentials(self, monkeypatch):
        """A4.9 R2-I2：用户级覆盖可同 URL/模型、不同 key——能力判定不得跨凭据复用。"""
        from app.model_gateway import vision_probe
        assert (vision_probe._cache_key(_ep(api_key="key-a"))
                != vision_probe._cache_key(_ep(api_key="key-b")))
        calls = []

        class _Keyed:
            def __init__(self, key):
                self.key = key

            async def complete_chat_parts(self, messages, **kwargs):
                calls.append(self.key)
                if self.key == "bad-key":
                    raise RuntimeError("401 invalid api key")
                return "K7M2QX green", ""

        monkeypatch.setattr("app.model_gateway.factory.build_llm_service",
                            lambda ep_, **k: _Keyed(ep_.api_key))
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))
        assert _run(vision_probe.probe_vision(_ep(api_key="bad-key"))) is False
        assert _run(vision_probe.probe_vision(_ep(api_key="good-key"))) is True
        assert calls == ["bad-key", "good-key"], "负例不得污染另一凭据"

    def test_concurrent_probes_single_flight(self, monkeypatch):
        """A4.9 R2-I1：并行视觉调用（vision_interpret 是 parallel-safe）只探一次。"""
        from app.model_gateway import vision_probe
        calls = []

        class _Slow:
            async def complete_chat_parts(self, messages, **kwargs):
                calls.append(1)
                await asyncio.sleep(0.05)
                return "K7M2QX green", ""

        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", lambda ep_, **k: _Slow())
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))

        async def _gather():
            return await asyncio.gather(*[vision_probe.probe_vision(_ep()) for _ in range(6)])

        assert all(_run(_gather()))
        assert len(calls) == 1, f"并发首探必须单飞，实际 {len(calls)} 次"


# ── vision_interpret 接线 ────────────────────────────────────────────────

def _make_ws():
    td = tempfile.mkdtemp()
    with open(os.path.join(td, "a.png"), "wb") as fh:
        fh.write(_TINY_PNG)
    return td


class TestProbeErrorTaxonomy:
    """A4.9 R2-I2：只有「拒绝图片输入」(400/415/422) 才是确定性无视觉；
    鉴权/模型不存在/超时/5xx 属不确定（不缓存、不伪装成能力结论）。"""

    def test_auth_error_is_indeterminate_not_cached(self, monkeypatch):
        from app.model_gateway import vision_probe
        calls = []

        class _Unauthorized(Exception):
            status_code = 401

        class _Reject:
            async def complete_chat_parts(self, messages, **kwargs):
                calls.append(1)
                raise _Unauthorized("invalid api key")

        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", lambda ep_, **k: _Reject())
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))
        assert _run(vision_probe.probe_vision(_ep(api_key="bad"))) is False
        assert vision_probe._CACHE == {}, "401 不得被缓存成确定性「无视觉」"
        assert _run(vision_probe.probe_vision(_ep(api_key="bad"))) is False
        assert len(calls) == 2, "不确定结果下次必须重探"

    def test_rejected_image_400_is_definitive_and_cached(self, monkeypatch):
        from app.model_gateway import vision_probe
        calls = []

        class _BadImage(Exception):
            status_code = 400

        class _Reject:
            async def complete_chat_parts(self, messages, **kwargs):
                calls.append(1)
                raise _BadImage("unsupported content type")

        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", lambda ep_, **k: _Reject())
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))
        assert _run(vision_probe.probe_vision(_ep())) is False
        assert _run(vision_probe.probe_vision(_ep())) is False
        assert len(calls) == 1, "400（拒绝图片）应确定性缓存"

    def test_concurrent_indeterminate_probes_share_one_call(self, monkeypatch):
        """A4.9 R2-I1：不确定结果也共享首探——否则 N 并发退化成 N×60s。"""
        from app.model_gateway import vision_probe
        calls = []

        class _Hang:
            async def complete_chat_parts(self, messages, **kwargs):
                calls.append(1)
                await asyncio.sleep(0.05)
                raise asyncio.TimeoutError("probe timeout")

        monkeypatch.setattr("app.model_gateway.factory.build_llm_service", lambda ep_, **k: _Hang())
        monkeypatch.setattr(vision_probe, "make_probe_png",
                            lambda: (b"\x89PNG\r\n\x1a\n", "K7M2QX", "green"))

        async def _gather():
            return await asyncio.gather(*[vision_probe.probe_vision(_ep()) for _ in range(8)])

        assert all(v is False for v in _run(_gather()))
        assert len(calls) == 1, f"并发不确定探针必须只打一次上游，实际 {len(calls)} 次"


def test_vision_interpret_uses_main_model_when_probe_capable(monkeypatch):
    from app.tools import vision_interpret as vi_mod
    main = _ep(alias="main", model_name="main-vlm")
    captured = {}

    class _FakeStreamLLM:
        async def stream_chat_structured(self, messages, **kwargs):
            captured["messages"] = messages
            yield {"type": "content", "data": "主模型看到了一只猫。"}
            yield {"type": "done", "data": {"finish_reason": "stop"}}

    async def _resolve(assistant=None):
        return main, "main_model"

    monkeypatch.setattr(vi_mod, "_resolve_vision", _resolve)

    def _fake_build(ep_, **k):
        captured["api_key"] = ep_.api_key
        return _FakeStreamLLM()

    monkeypatch.setattr("app.model_gateway.factory.build_llm_service", _fake_build)
    ws = _make_ws()
    out = json.loads(_run(vi_mod.vision_interpret(
        {"image_path": "a.png", "question": "图里有什么？"}, workspace_path=ws)))
    assert out.get("success") is True, out
    assert out["interpretation"] == "主模型看到了一只猫。"
    assert out["model"] == "main-vlm"
    assert out["vision_source"] == "main_model"
    assert captured["api_key"] == "no-key", "空 key 主模型也不得回落系统密钥"
    assert captured["messages"][0]["content"][1]["type"] == "image_url"


def test_vision_interpret_no_vision_friendly_error(monkeypatch):
    from app.tools import vision_interpret as vi_mod

    async def _resolve(assistant=None):
        return None, "no_vision"

    monkeypatch.setattr(vi_mod, "_resolve_vision", _resolve)
    ws = _make_ws()
    out = json.loads(_run(vi_mod.vision_interpret(
        {"image_path": "a.png", "question": "q"}, workspace_path=ws)))
    assert out.get("success") is False
    assert "视觉能力" in out["error"], out
