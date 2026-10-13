"""TDD tests: 缓办收尾 + ASR/TTS 端点池化 + agent 语音工具 + config_model 类型池重构.

Criteria map: tests/acceptance.md（2026-08-31 第二波）items 1-19.
Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_config_pool_refactor.py -v
"""
import asyncio
import base64
import json
import os
import sys
import tempfile
import tomllib
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.model_gateway.registry import ModelRegistry
from app.model_gateway.schemas import ModelEndpoint, KIND_LLM, KIND_ASR, KIND_TTS

BACKEND_ROOT = os.path.dirname(os.path.abspath(__file__)) + "/.."

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
_TINY_WAV = b"RIFF" + b"\x00" * 64  # 内容无关紧要（mock 服务）；仅扩展名校验用


def _run(coro):
    return asyncio.run(coro)


def _patch_asr_conf(monkeypatch, asr_conf: dict) -> None:
    """_asr_config 是 property（不可 setattr）→ patch config 单例的 [asr] 段。"""
    from app.core.config import get_config
    monkeypatch.setitem(get_config()._config, "asr", asr_conf)


def _registry_with(asr_ep: ModelEndpoint) -> ModelRegistry:
    conf = {"endpoints": {"asr": {
        "kind": "asr", "base_url": asr_ep.base_url, "api_key": asr_ep.api_key,
        "model_name": asr_ep.model_name, "provider_type": asr_ep.provider_type,
        "extra": dict(asr_ep.extra),
    }}}
    return ModelRegistry.from_sources(conf)


# ══ Wave A — 缓办收尾 ═══════════════════════════════════════════════════

def _conv_for_prompt(**over):
    base = dict(
        id="conv-pool",
        deathmatch_mode=True,
        deathmatch_status="active",
        deathmatch_goal="测试目标",
        deathmatch_turns=3,
        deathmatch_max_turns=0,
        deathmatch_verify_failures=0,
        deathmatch_consecutive_failures=0,
        deathmatch_verdict=None,
        deathmatch_reason=None,
        deathmatch_plan={"steps": [
            {"id": "s1", "status": "in_progress", "description": "步骤1", "expected_output": "a.txt"},
        ]},
        deathmatch_plan_version=1,
        deathmatch_reflections=[],
        deathmatch_last_verification_result=None,
        deathmatch_wall_time_started_at=None,
        deathmatch_max_wall_time_seconds=0,
        deathmatch_wall_time_used_seconds=0,
        deathmatch_grilling_qa_history=[],
        deathmatch_grilling_complete=True,
        deathmatch_human_gate=None,
        deathmatch_bible_draft=None,
        deathmatch_settled_ledger=None,
        deathmatch_subgoals=[],
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_turn_label_unlimited_no_fake_budget():
    """判据 1a：max_turns=0（不限）时模板不得出现「/0轮」「共30轮」。"""
    import app.services.deathmatch_service as ds
    conv = _conv_for_prompt(deathmatch_max_turns=0)
    prompt = ds.DeathmatchManager(conv).get_continuation_prompt("working") or ""
    assert "/0轮" not in prompt and "共30轮" not in prompt, prompt[:200]
    assert "不限" in prompt, prompt[:200]


def test_turn_label_limited_keeps_budget():
    """判据 1b：max_turns>0 时仍显示「共M轮」。"""
    import app.services.deathmatch_service as ds
    conv = _conv_for_prompt(deathmatch_max_turns=30)
    prompt = ds.DeathmatchManager(conv).get_continuation_prompt("working") or ""
    assert "共30轮" in prompt, prompt[:200]


def test_vlm_error_kinds_distinguished(monkeypatch):
    """判据 2：registry 抛错（配置加载失败）≠ 未配置——文案区分 + 日志 exc_info。"""
    from app.tools import vision_interpret as vi_mod

    def _boom():
        raise RuntimeError("toml broken")
    fake_registry = type("R", (), {"resolve": staticmethod(lambda purpose: _boom())})()
    monkeypatch.setattr("app.model_gateway.registry.get_model_registry", lambda: fake_registry)
    ws_dir = tempfile.mkdtemp()
    with open(os.path.join(ws_dir, "a.png"), "wb") as fh:
        fh.write(_TINY_PNG)
    out = json.loads(_run(vi_mod.vision_interpret(
        {"image_path": "a.png", "question": "q"}, workspace_path=ws_dir)))
    assert out.get("success") is False
    assert "加载失败" in out["error"], out
    assert "未配置" not in out["error"], out


def test_consolidate_keeps_routing_when_both_present(tmp_path):
    """判据 3：deathmatch.vlm 与 vlm 并存（改名未发生）→ routing 原指向保留。"""
    from scripts.migrate_model_registry import build_consolidation
    model = tmp_path / "config_model.toml"
    model.write_text('[api]\nmodel_name = "x"\n', encoding="utf-8")
    registry = tmp_path / "model_registry.toml"
    registry.write_text("""
[endpoints."deathmatch.vlm"]
kind = "llm"
base_url = "http://legacy/v1"
api_key = "sk-l"
model_name = "mm-1"

[endpoints.vlm]
kind = "llm"
base_url = "http://canon/v1"
api_key = "sk-c"
model_name = "mm-2"

[routing."vlm"]
alias = "deathmatch.vlm"
""", encoding="utf-8")
    block, summary = build_consolidation(model, registry)
    doc = tomllib.loads(block)
    assert doc["routing"]["vlm"]["alias"] == "deathmatch.vlm", (
        "改名未发生时不得改写 routing（A4.9 W3-R2 deferred edge）"
    )


# ══ Wave B — ASR/TTS 端点池化 + agent 语音工具 ══════════════════════════

def test_asr_provider_from_endpoint(monkeypatch):
    from app.services.asr_service import ASRService
    ep = ModelEndpoint(alias="asr", kind=KIND_ASR, provider_type="mimo",
                       base_url="http://mimo:9000", api_key="sk-m", model_name="qwen3-asr")
    monkeypatch.setattr("app.model_gateway.registry.get_model_registry",
                        lambda: _registry_with(ep))
    svc = ASRService()
    assert svc.provider == "mimo"
    assert svc.is_mimo is True and svc.is_dashscope is False


def test_asr_provider_fallback_config_key(monkeypatch):
    from app.services.asr_service import ASRService
    ep = ModelEndpoint(alias="asr", kind=KIND_ASR, provider_type="")
    monkeypatch.setattr("app.model_gateway.registry.get_model_registry",
                        lambda: _registry_with(ep))
    svc = ASRService()
    _patch_asr_conf(monkeypatch, {"asr_provider": "general"})
    assert svc.provider == "general"


def test_asr_provider_legacy_booleans_then_default(monkeypatch):
    from app.services.asr_service import ASRService
    ep = ModelEndpoint(alias="asr", kind=KIND_ASR, provider_type="")
    monkeypatch.setattr("app.model_gateway.registry.get_model_registry",
                        lambda: _registry_with(ep))
    svc = ASRService()
    _patch_asr_conf(monkeypatch, {"is_mimo": True})
    assert svc.provider == "mimo"
    _patch_asr_conf(monkeypatch, {"is_dashscope": True, "is_mimo": False})
    assert svc.provider == "dashscope"
    _patch_asr_conf(monkeypatch, {})
    assert svc.provider == "dashscope", "缺省默认 dashscope（U1）"


def test_asr_single_key_principle(monkeypatch):
    from app.services.asr_service import ASRService
    ep = ModelEndpoint(alias="asr", kind=KIND_ASR, provider_type="dashscope",
                       api_key="sk-top", model_name="fun-asr-realtime",
                       extra={"dashscope_api_key": "sk-extra"})
    monkeypatch.setattr("app.model_gateway.registry.get_model_registry",
                        lambda: _registry_with(ep))
    svc = ASRService()
    assert svc.dashscope_api_key == "sk-top", "ep.api_key 优先（单一密钥）"
    assert svc.dashscope_model == "fun-asr-realtime", "ep.model_name 优先"
    ep2 = ModelEndpoint(alias="asr", kind=KIND_ASR, provider_type="dashscope",
                        api_key="", model_name="",
                        extra={"dashscope_api_key": "sk-extra", "dashscope_model": "fun-asr-realtime"})
    monkeypatch.setattr("app.model_gateway.registry.get_model_registry",
                        lambda: _registry_with(ep2))
    svc2 = ASRService()
    assert svc2.dashscope_api_key == "sk-extra", "旧显式端点（key 在 extra）仍兜底"
    assert svc2.dashscope_model == "fun-asr-realtime"


def test_tts_voice_style_endpoint_first(monkeypatch):
    import app.services.tts_service as tts_mod
    ep = ModelEndpoint(alias="tts", kind=KIND_TTS, base_url="http://tts/v1",
                       api_key="sk-t", model_name="mimo-v2.5-tts",
                       extra={"voice": "糖糖", "style_instruction": "端点风格"})
    fake_registry = type("R", (), {"get": staticmethod(lambda alias: ep)})()
    monkeypatch.setattr("app.model_gateway.registry.get_model_registry", lambda: fake_registry)
    svc = tts_mod.TTSService()
    assert svc.voice == "糖糖"
    assert svc._resolve_style(None) == "端点风格"
    assert svc._resolve_style("显式") == "显式"


# ══ 判据 8：语音子用途路由 + 助手继承兼容 ═══════════════════════════════

def _voice_reg(conf_routing=None):
    conf = {
        "api": {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-main", "model_name": "deepseek-chat"},
        "endpoints": {
            "main": {"kind": "llm", "base_url": "https://api.deepseek.com/v1", "api_key": "sk-main",
                     "model_name": "deepseek-chat"},
            "mimo": {"kind": "llm", "base_url": "http://mimo/v1", "api_key": "sk-m", "model_name": "mimo-l"},
        },
    }
    if conf_routing:
        conf["routing"] = conf_routing
    return ModelRegistry.from_sources(conf)


class _FakeSession:
    """_build_llm 的最小 self：只需 _voice_assistant。"""

    def __init__(self, asst):
        self._voice_assistant = asst


def test_voice_subpurposes_explicit_routing_wins(monkeypatch):
    from app.services.voice_service import VoiceDuplexSession
    reg = _voice_reg({"voice.duplex": {"alias": "mimo", "is_custom": True}})
    monkeypatch.setattr("app.model_gateway.registry.get_model_registry", lambda: reg)
    asst = SimpleNamespace(model_alias="", custom_model_name=None, use_custom_model=False,
                           provider_type="deepseek")
    svc, model_name = VoiceDuplexSession._build_llm(_FakeSession(asst), purpose="voice.duplex")
    assert "mimo" in str(svc.client.base_url), "显式 routing=池成员例外"


def test_voice_subpurposes_inherit_assistant_without_routing(monkeypatch):
    """无显式 voice.* routing：子分类继承助手端点（助手全链路继承反馈）。"""
    from app.services.voice_service import VoiceDuplexSession
    reg = _voice_reg()
    monkeypatch.setattr("app.model_gateway.registry.get_model_registry", lambda: reg)
    asst = SimpleNamespace(model_alias="mimo", custom_model_name=None, use_custom_model=False,
                           provider_type="deepseek")
    svc, _ = VoiceDuplexSession._build_llm(_FakeSession(asst), purpose="voice.intent")
    assert "mimo" in str(svc.client.base_url), "无显式子路由 → 继承助手端点"


def test_voice_legacy_overrides_become_explicit_routing(monkeypatch):
    """legacy [voice] duplex_model 非空 → synthesize 升格为显式子路由。"""
    conf = {
        "api": {"base_url": "https://api.deepseek.com/v1", "api_key": "sk", "model_name": "deepseek-chat"},
        "voice": {"duplex_model": "mimo-l"},
        "providers": {},
    }
    reg = ModelRegistry.from_sources(conf)
    assert reg.has_explicit_routing("voice.duplex")
    ep = reg.resolve("voice.duplex")
    assert ep.model_name == "mimo-l"


# ══ 判据 9/10：agent 语音工具 ═══════════════════════════════════════════

def test_voice_tools_registered():
    import app.tools  # noqa: F401
    from app.tools.registry import registry
    names = {d["function"]["name"] for d in registry.get_definitions()}
    assert "asr_transcribe" in names and "tts_synthesize" in names


def test_asr_transcribe_tool(monkeypatch):
    from app.tools import voice_tools
    td = tempfile.mkdtemp()
    with open(os.path.join(td, "a.wav"), "wb") as fh:
        fh.write(_TINY_WAV)
    captured = {}

    class _FakeASR:
        enabled = True

        async def transcribe_file(self, data, filename="", language="auto"):
            captured["filename"] = filename
            return {"text": "你好世界", "language": "zh"}

    monkeypatch.setattr(voice_tools, "_get_asr_service", lambda: _FakeASR())
    out = json.loads(_run(voice_tools.asr_transcribe(
        {"audio_path": "a.wav"}, workspace_path=td)))
    assert out.get("success") is True and out["text"] == "你好世界"
    out2 = json.loads(_run(voice_tools.asr_transcribe(
        {"audio_path": "../escape.wav"}, workspace_path=td)))
    assert out2.get("success") is False
    with open(os.path.join(td, "b.txt"), "w") as fh:
        fh.write("x")
    out3 = json.loads(_run(voice_tools.asr_transcribe(
        {"audio_path": "b.txt"}, workspace_path=td)))
    assert out3.get("success") is False and "音频" in out3["error"]


def test_tts_synthesize_tool(monkeypatch):
    from app.tools import voice_tools
    td = tempfile.mkdtemp()

    class _FakeTTS:
        voice = "冰糖"
        model = "mimo-v2.5-tts"

        def is_configured(self):
            return True

        async def stream_tts(self, text, style_instruction=None, voice=None, timeout=60.0):
            assert text == "你好"
            yield b"\x01\x02" * 100

    monkeypatch.setattr(voice_tools, "_get_tts_service", lambda: _FakeTTS())
    out = json.loads(_run(voice_tools.tts_synthesize(
        {"text": "你好"}, workspace_path=td)))
    assert out.get("success") is True, out
    assert out["path"].endswith(".wav") and os.path.isfile(os.path.join(td, out["path"]))
    assert out["audio_bytes"] > 0


def test_tts_synthesize_empty_text_rejected():
    from app.tools import voice_tools
    out = json.loads(_run(voice_tools.tts_synthesize({"text": "  "}, workspace_path="/tmp")))
    assert out.get("success") is False


# ══ Wave C — config_model 类型池重构（文件 doctrine）════════════════════

_REAL_CONF = os.path.join(BACKEND_ROOT, "config_model.toml")
_requires_real_conf = pytest.mark.skipif(
    not os.path.isfile(_REAL_CONF), reason="config_model.toml is gitignored (fresh clone)")


def _load_real_model_conf() -> dict:
    with open(_REAL_CONF, "rb") as fh:
        return tomllib.load(fh)


@_requires_real_conf
def test_file_doctrine_no_legacy_endpoint_sections():
    """判据 12a：端点定义只存在于端点池——[api]/[providers]/[deathmatch.judge] 退役。"""
    conf = _load_real_model_conf()
    assert "api" not in conf, "[api] 与 [endpoints.main] 重复，必须删除"
    assert "providers" not in conf, "[providers.*] 与端点池重复，必须删除"
    dm = conf.get("deathmatch", {})
    assert "judge" not in dm, "[deathmatch.judge] 空段与 [routing.\"deathmatch.judge\"] 重复"


@_requires_real_conf
def test_file_doctrine_asr_voice_memory_behavior_only():
    """判据 12b：[asr]/[voice]/[memory] 只留行为参数，端点键清零。"""
    conf = _load_real_model_conf()
    asr = conf.get("asr", {})
    for key in ("is_dashscope", "is_mimo", "api_key", "base_url", "model",
                "dashscope_api_key", "dashscope_model", "dashscope_vocabulary_url"):
        assert key not in asr, f"[asr].{key} 是端点键，必须只在 [endpoints.asr]"
    voice = conf.get("voice", {})
    for key in ("provider", "model_name", "duplex_model", "intent_model",
                "interjection_model", "memory_interjection_model",
                "tts_base_url", "tts_api_key", "tts_model", "tts_voice",
                "tts_style_instruction"):
        assert key not in voice, f"[voice].{key} 是模型选择/端点键，必须在端点池或 [routing]"
    mem = conf.get("memory", {})
    for key in ("embedding_model", "embedding_api_base", "embedding_api_key",
                "embedding_dim", "rerank_model", "rerank_api_base", "rerank_api_key",
                "concept_extraction_model", "dream_model", "clarification_model",
                "query_expansion_model"):
        assert key not in mem, f"[memory].{key} 是端点/选择键，必须在端点池或 [routing]"


@_requires_real_conf
def test_file_doctrine_pool_uniqueness():
    """判据 13：端点池每端点唯一——无 qwen3.8_vllm 镜像；Qwen3.8(Local) 显示名。"""
    conf = _load_real_model_conf()
    eps = conf.get("endpoints", {})
    assert "qwen3.8_vllm" not in eps, "qwen3.8_vllm 是 qwen3.8 的镜像重复"
    assert eps.get("qwen3.8", {}).get("display_name") == "Qwen3.8(Local)"
    assert eps.get("asr", {}).get("provider_type") in ("dashscope", "mimo", "general")
    assert eps.get("main", {}).get("is_custom") is True, (
        "main 自包含（is_custom=true + params），[api] 移除后裸构造不再依赖"
    )


@_requires_real_conf
def test_registry_from_real_file_pool():
    """真实文件 → registry：池成员/别名/子路由链行为正确。"""
    conf = _load_real_model_conf()
    reg = ModelRegistry.from_sources(conf)
    assert reg.has("qwen3.8") and not reg.has("qwen3.8_vllm"), "镜像别名退役"
    assert reg.get("qwen3.8").display_name == "Qwen3.8(Local)"
    assert reg.get("asr").provider_type == "dashscope"
    assert reg.get("asr").api_key, "ASR 单一密钥在端点顶层"
    assert reg.resolve("vlm") is not None
    public = {a["alias"] for a in reg.public_aliases()}
    assert "qwen3.8_vllm" not in public and "vlm" not in public


@_requires_real_conf
def test_main_endpoint_selfcontained_sampling():
    """main is_custom=true 后采样由 params 自包含（与原 [defaults] 值一致）。"""
    conf = _load_real_model_conf()
    main = conf["endpoints"]["main"]
    assert main["is_custom"] is True
    params = main.get("params", {})
    assert params.get("temperature") == 0.7 and params.get("top_p") == 0.9


def test_config_api_fallback_to_main_endpoint(monkeypatch):
    """判据 15：[api] 缺失时 config.api_* 懒回退 main 端点（裸构造保护）。"""
    from app.core.config import get_config
    cfg = get_config()
    monkeypatch.setitem(cfg._config, "api", {})
    val = cfg.api_base_url
    assert val and "openai.com" not in val, f"必须回退 main 端点，got {val!r}"
    assert cfg.model_name, "model_name 懒回退"


# ══ A4.9 W-2 Imp-4：transcribe_file dashscope 一次性 WS 协议钉 ═══════════

class _FakeUpstream:
    """脚本化 Fun-ASR WS 上游：校验 run-task/finish-task，回放事件。"""

    def __init__(self, script, fail_task=False):
        self._script = list(script)
        self.sent = []
        self.fail_task = fail_task

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, msg):
        self.sent.append(msg)
        if isinstance(msg, str):
            import json as _json
            d = _json.loads(msg)
            if d.get("header", {}).get("action") == "finish-task":
                if self.fail_task:
                    self._script.append({"header": {"event": "task-failed",
                                                    "error_message": "boom"}})
                self._script.extend([
                    {"header": {"event": "result-generated"},
                     "payload": {"output": {"sentence": {"text": "今天天气很好，", "sentence_end": True}}}},
                    {"header": {"event": "task-finished"}},
                ])

    async def recv(self):
        if not self._script:
            raise RuntimeError("script exhausted")
        item = self._script.pop(0)
        return item if isinstance(item, str) else json.dumps(item)


def _oneshot_harness(monkeypatch, script, fail_task=False, pcm=b"\x01\x02" * 50):
    import app.services.asr_service as asr_mod
    svc = asr_mod.ASRService()
    monkeypatch.setattr(asr_mod.ASRService, "_convert_audio_to_raw_pcm16",
                        lambda self, data, filename="": pcm, raising=False)
    upstream = _FakeUpstream(script, fail_task=fail_task)
    monkeypatch.setattr(asr_mod.websockets, "connect",
                        lambda *a, **kw: _async_return(upstream))
    return svc, upstream


class _AsyncCtx:
    def __init__(self, upstream):
        self._u = upstream

    async def __aenter__(self):
        return self._u

    async def __aexit__(self, *a):
        return False


def _async_return(upstream):
    class _Awaitable:
        def __await__(self):
            if False:
                yield
            return upstream
    return _Awaitable()


def test_oneshot_happy_path(monkeypatch):
    svc, upstream = _oneshot_harness(monkeypatch, [
        {"header": {"event": "task-started"}},
    ])
    out = _run(svc._transcribe_dashscope_oneshot(b"xx", filename="a.wav", language="zh"))
    assert out["text"] == "今天天气很好，"
    assert out["language"] == "zh"
    # 协议形状：run-task → pcm 二进制帧 → finish-task
    actions = [json.loads(m)["header"]["action"] for m in upstream.sent if isinstance(m, str)]
    assert actions[0] == "run-task" and actions[-1] == "finish-task"
    run_task = json.loads(upstream.sent[0])
    assert run_task["payload"]["model"] == svc.dashscope_model
    assert any(not isinstance(m, str) for m in upstream.sent), "pcm 必须以二进制帧发送"


def test_oneshot_task_failed_raises(monkeypatch):
    svc, _ = _oneshot_harness(monkeypatch, [
        {"header": {"event": "task-started"}},
    ], fail_task=True)
    with pytest.raises(RuntimeError, match="boom"):
        _run(svc._transcribe_dashscope_oneshot(b"xx"))


def test_oneshot_empty_pcm_raises(monkeypatch):
    svc, _ = _oneshot_harness(monkeypatch, [], pcm=b"")
    with pytest.raises(RuntimeError, match="empty PCM"):
        _run(svc._transcribe_dashscope_oneshot(b"xx"))


def test_oneshot_provider_dispatch(monkeypatch):
    """mimo/general/dashscope 分派：mock 三个通道各一次。"""
    import app.services.asr_service as asr_mod
    svc = asr_mod.ASRService()
    calls = []

    async def fake_mimo(self, data, filename="", language="auto"):
        calls.append("mimo")
        return {"text": "m"}

    async def fake_general(self, data, filename=None, custom_hotwords=None):
        calls.append("general")
        return {"text": "g"}

    async def fake_dash(self, data, filename="", language="auto"):
        calls.append("dash")
        return {"text": "d"}

    monkeypatch.setattr(asr_mod.ASRService, "is_mimo",
                        property(lambda self: True), raising=False)
    monkeypatch.setattr(asr_mod.ASRService, "enabled",
                        property(lambda self: True), raising=False)
    monkeypatch.setattr(asr_mod.ASRService, "_transcribe_mimo", fake_mimo, raising=False)
    _run(svc.transcribe_file(b"x"))
    monkeypatch.setattr(asr_mod.ASRService, "is_mimo",
                        property(lambda self: False), raising=False)
    monkeypatch.setattr(asr_mod.ASRService, "is_dashscope",
                        property(lambda self: True), raising=False)
    monkeypatch.setattr(asr_mod.ASRService, "_transcribe_dashscope_oneshot", fake_dash, raising=False)
    _run(svc.transcribe_file(b"x"))
    monkeypatch.setattr(asr_mod.ASRService, "is_dashscope",
                        property(lambda self: False), raising=False)
    monkeypatch.setattr(asr_mod.ASRService, "enabled",
                        property(lambda self: True), raising=False)
    monkeypatch.setattr(asr_mod.ASRService, "transcribe", fake_general, raising=False)
    _run(svc.transcribe_file(b"x"))
    assert calls == ["mimo", "dash", "general"]


# ══ Wave D — vector 维度对账修复（判据 20-23）═══════════════════════════

def test_expected_dim_from_endpoint_extra(monkeypatch):
    """判据 20：期望维度 = [endpoints.embedding].extra.dim（池 doctrine 源）。"""
    from app.db import migrations as mig
    import app.model_gateway.registry as reg_mod
    from app.model_gateway.schemas import ModelEndpoint, KIND_EMBEDDING
    ep = ModelEndpoint(alias="embedding", kind=KIND_EMBEDDING,
                       base_url="http://e/v1", api_key="", model_name="Qwen3-Embedding",
                       extra={"dim": 1024})
    monkeypatch.setattr(reg_mod, "get_model_registry",
                        lambda: type("R", (), {"get": staticmethod(lambda a: ep)})())
    conf = {"endpoints": {"embedding": {"kind": "embedding", "base_url": "http://e/v1",
                                        "api_key": "", "model_name": "Qwen3-Embedding",
                                        "extra": {"dim": 1024}}}}
    assert mig._expected_embedding_dim(conf) == 1024


def test_expected_dim_legacy_memory_key_fallback(monkeypatch):
    from app.db import migrations as mig
    import app.model_gateway.registry as reg_mod
    monkeypatch.setattr(reg_mod, "get_model_registry",
                        lambda: (_ for _ in ()).throw(KeyError("isolated")))
    conf = {"memory": {"embedding_dim": 1536}}
    assert mig._expected_embedding_dim(conf) == 1536


def test_expected_dim_unknown_returns_zero_never_1536_default(monkeypatch):
    """判据 20 核心：两端点键都缺失时=0（对账跳过），绝不默认 1536。"""
    from app.db import migrations as mig
    import app.model_gateway.registry as reg_mod
    monkeypatch.setattr(reg_mod, "get_model_registry",
                        lambda: (_ for _ in ()).throw(KeyError("isolated")))
    assert mig._expected_embedding_dim({}) == 0
    assert mig._expected_embedding_dim({"memory": {}}) == 0


def test_reconcile_skips_when_expected_unknown(monkeypatch):
    """期望维度未知 → 不执行任何 ALTER（防 1536 默认回归）。"""
    import app.db.migrations as mig
    import app.core.config as cfg_mod
    import app.model_gateway.registry as reg_mod

    class _FakeConn:
        def begin_nested(self):
            raise AssertionError("must not touch DB when expected unknown")

    monkeypatch.setattr(cfg_mod, "get_config",
                        lambda: type("C", (), {"_config": {"memory": {}}})())
    monkeypatch.setattr(reg_mod, "get_model_registry",
                        lambda: (_ for _ in ()).throw(KeyError("isolated")))
    _run(mig._reconcile_vector_dims(_FakeConn()))
    # 未抛断言异常 = 未触碰 DB（skip 生效）


def test_composed_synthesize_legacy_conf_never_yields_1536(monkeypatch):
    """C1 组合回归：legacy 形态配置（无 endpoints/memory_dim）经 synthesize
    → _expected_embedding_dim 必须=0（绝不默认 1536）。"""
    from app.db import migrations as mig
    from app.model_gateway.registry import ModelRegistry
    reg = ModelRegistry.from_sources({"memory": {}})
    monkeypatch.setattr("app.model_gateway.registry.get_model_registry", lambda: reg)
    assert mig._expected_embedding_dim({"memory": {}}) == 0


def test_kwargs_empty_ext_params_not_on_wire(monkeypatch):
    """判据 21：kwargs 侧 top_k/min_p/repetition_penalty 空值不上 wire。"""
    from app.model_gateway import factory
    from app.model_gateway.registry import ModelRegistry
    reg = ModelRegistry.from_sources({
        "api": {"base_url": "https://api.deepseek.com/v1", "api_key": "sk", "model_name": "m"},
        "endpoints": {"main": {"kind": "llm", "base_url": "https://api.deepseek.com/v1",
                               "api_key": "sk", "model_name": "m"}},
    })
    svc = factory.build_llm_service(reg.get("main"))
    params = svc._build_params([{"role": "user", "content": "hi"}],
                               temperature=0.7, top_k="", min_p="", repetition_penalty="")
    eb = params.get("extra_body") or {}
    assert "top_k" not in eb and "min_p" not in eb and "repetition_penalty" not in eb, eb
    # 合法值仍透传
    params2 = svc._build_params([{"role": "user", "content": "hi"}],
                                temperature=0.7, top_k=40)
    assert (params2.get("extra_body") or {}).get("top_k") == 40


def test_synthesize_asr_empty_model_provider_defaults(monkeypatch):
    """判据 22（Wave E 更新）：显式 model="" → 按 provider 的正确默认——
    dashscope=实时模型、mimo=mimo-v2.5-asr；早期遗留 qwen3-asr 不再出现。"""
    from app.model_gateway.registry import synthesize
    _dash = synthesize({"asr": {"model": "", "is_dashscope": True,
                                "dashscope_api_key": "sk", "dashscope_model": ""}})
    _mimo = synthesize({"asr": {"model": "", "is_mimo": True, "api_key": "sk"}})
    assert _dash[0]["asr"].model_name == "qwen3-asr-flash-realtime-2026-02-10"
    assert _mimo[0]["asr"].model_name == "mimo-v2.5-asr"


# ══ Wave E — 清除早期 qwen3-asr 遗留默认（用户纠正 2026-09-01）══════════

def test_mimo_default_model_is_mimo_v25_asr(monkeypatch):
    """用户纠正：qwen3-asr 是早期方案遗留；MiMo 默认 = mimo-v2.5-asr。"""
    from app.model_gateway.registry import synthesize
    endpoints, _ = synthesize({"asr": {"is_mimo": True, "api_key": "sk-m"}})
    assert endpoints["asr"].model_name == "mimo-v2.5-asr", (
        f"MiMo 空模型默认错: {endpoints['asr'].model_name!r}"
    )


def test_general_default_model_is_paraformer_zh(monkeypatch):
    from app.model_gateway.registry import synthesize
    endpoints, _ = synthesize({"asr": {"base_url": "http://general:8000"}})
    assert endpoints["asr"].model_name == "paraformer-zh"


def test_asr_service_model_default_provider_aware(monkeypatch):
    from app.services.asr_service import ASRService
    from app.model_gateway.registry import ModelRegistry
    from app.model_gateway.schemas import ModelEndpoint, KIND_ASR
    ep = ModelEndpoint(alias="asr", kind=KIND_ASR, provider_type="mimo",
                       base_url="http://mimo:9000", api_key="sk-m", model_name="")
    monkeypatch.setattr("app.model_gateway.registry.get_model_registry",
                        lambda: _registry_with(ep))
    svc = ASRService()
    _patch_asr_conf(monkeypatch, {})
    assert svc.model == "mimo-v2.5-asr", "MiMo 空模型默认 = mimo-v2.5-asr"


def test_no_stale_qwen3_asr_default_in_code():
    """qwen3-asr 早期方案默认不得再出现在代码默认值里（dashscope realtime 除外）。"""
    import re
    for f in ("app/services/asr_service.py", "app/model_gateway/registry.py"):
        src = open(os.path.join(BACKEND_ROOT, f)).read()
        hits = [m for m in re.findall(r'["\']qwen3-asr["\']', src)]
        assert not hits, f"{f} 仍残留早期 qwen3-asr 默认: {hits}"
