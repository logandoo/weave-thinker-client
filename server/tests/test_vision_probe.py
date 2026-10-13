"""TDD — 视觉能力探针（model_gateway/vision_probe.py）+ vision_interpret 接线。

需求（2026-09-15）：视觉模型（vlm）未单独配置时，视觉任务先对当前主模型做
一次行为探针（随机 token + 色块图）；有视觉 → 主模型充当视觉模型；无视觉 →
明确失败。探针结果进程内缓存。

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
    yield
    vision_probe._CACHE.clear()


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
        key = "http://m.local/v1|m-1"
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

    def test_configured_vlm_wins_without_probe(self, monkeypatch):
        from app.model_gateway import vision_probe
        vlm = _ep(alias="vlm", base_url="http://vlm.local/v1", model_name="vlm-1")
        main = _ep(alias="main")
        monkeypatch.setattr("app.model_gateway.registry.get_model_registry",
                            lambda: self._fake_registry(vlm, main))

        async def _boom(ep):
            raise AssertionError("configured vlm must not trigger a probe")

        monkeypatch.setattr(vision_probe, "probe_vision", _boom)
        ep, reason = _run(vision_probe.resolve_vision_endpoint())
        assert ep is vlm and reason == "configured"

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


# ── vision_interpret 接线 ────────────────────────────────────────────────

def _make_ws():
    td = tempfile.mkdtemp()
    with open(os.path.join(td, "a.png"), "wb") as fh:
        fh.write(_TINY_PNG)
    return td


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
    monkeypatch.setattr("app.model_gateway.factory.build_llm_service",
                        lambda ep_, **k: _FakeStreamLLM())
    ws = _make_ws()
    out = json.loads(_run(vi_mod.vision_interpret(
        {"image_path": "a.png", "question": "图里有什么？"}, workspace_path=ws)))
    assert out.get("success") is True, out
    assert out["interpretation"] == "主模型看到了一只猫。"
    assert out["model"] == "main-vlm"
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
