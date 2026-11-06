"""TDD tests: vision_interpret 工具（VLM 能力开放给 agent）+ registry canonical vlm 别名.

Criteria map: tests/acceptance.md items 17-21, 24.
Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_vision_tool.py -v
"""
import asyncio
import base64
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.model_gateway.registry import ModelRegistry, synthesize
from app.model_gateway.schemas import ModelEndpoint, KIND_LLM

# 1x1 transparent PNG
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _run(coro):
    return asyncio.run(coro)


# ── Criterion 17: tool registered with schema ────────────────────────────

def test_registered_and_schema():
    import app.tools  # noqa: F401 — triggers tool auto-discovery
    from app.tools.registry import registry
    defs = registry.get_definitions()
    names = {d["function"]["name"] for d in defs}
    assert "vision_interpret" in names, "vision_interpret must be registered"
    schema = next(d for d in defs if d["function"]["name"] == "vision_interpret")["function"]
    props = schema["parameters"]["properties"]
    assert "image_path" in props and "question" in props
    assert "image_path" in schema["parameters"]["required"]


# ── Criterion 21: parallel-safe ──────────────────────────────────────────

def test_parallel_safe_membership():
    from app.services.agent_loop import _PARALLEL_SAFE_TOOLS
    assert "vision_interpret" in _PARALLEL_SAFE_TOOLS


# ── Tool handler behavior ────────────────────────────────────────────────

def _make_ws():
    td = tempfile.mkdtemp()
    with open(os.path.join(td, "a.png"), "wb") as fh:
        fh.write(_TINY_PNG)
    with open(os.path.join(td, "b.txt"), "w") as fh:
        fh.write("not an image")
    return td


def test_path_escape_rejected():
    from app.tools.vision_interpret import vision_interpret
    ws = _make_ws()
    out = json.loads(_run(vision_interpret(
        {"image_path": "../outside.png", "question": "q"}, workspace_path=ws)))
    assert out.get("success") is False and "error" in out


def test_non_image_rejected():
    from app.tools.vision_interpret import vision_interpret
    ws = _make_ws()
    out = json.loads(_run(vision_interpret(
        {"image_path": "b.txt", "question": "q"}, workspace_path=ws)))
    assert out.get("success") is False and "不支持" in out["error"]


def test_unconfigured_and_no_vision_friendly_error(monkeypatch):
    """vlm 未配置 + 主模型无视觉能力（空端点）→ 明确「无视觉能力」错误。"""
    from app.tools import vision_interpret as vi_mod
    empty_vlm = ModelEndpoint(alias="vlm", kind=KIND_LLM, base_url="", api_key="", model_name="")
    empty_main = ModelEndpoint(alias="main", kind=KIND_LLM, base_url="", api_key="", model_name="")

    class _FakeRegistry:
        def resolve(self, purpose):
            return empty_vlm

        def endpoint_for_assistant(self, assistant):
            return empty_main

    monkeypatch.setattr("app.model_gateway.registry.get_model_registry", lambda: _FakeRegistry())
    ws = _make_ws()
    out = json.loads(_run(vi_mod.vision_interpret(
        {"image_path": "a.png", "question": "q"}, workspace_path=ws)))
    assert out.get("success") is False
    assert "视觉能力" in out["error"], out


def test_mocked_gateway_call(monkeypatch):
    from app.tools import vision_interpret as vi_mod
    ep = ModelEndpoint(alias="vlm", kind=KIND_LLM, base_url="http://vlm.local/v1",
                       api_key="sk-x", model_name="qwen-vl-max")
    # 2026-11-01：解析顺序改为主模型探针优先——fixture 必须同时给出主端点
    # （此处主端点为空 = 无视觉），才能测到 vlm 回落路径。
    _empty_main = ModelEndpoint(alias="main", kind=KIND_LLM, base_url="", api_key="",
                                model_name="")
    fake_registry = type("R", (), {
        "resolve": lambda self, purpose: (_empty_main if purpose == "main" else ep),
        "endpoint_for_assistant": lambda self, assistant: _empty_main,
    })()
    monkeypatch.setattr("app.model_gateway.registry.get_model_registry", lambda: fake_registry)

    captured = {}

    class _FakeLLM:
        async def stream_chat_structured(self, messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            yield {"type": "content", "data": "图中是一只猫。"}
            yield {"type": "done", "data": {"finish_reason": "stop"}}

    monkeypatch.setattr("app.model_gateway.factory.build_llm_service", lambda ep_, **k: _FakeLLM())
    ws = _make_ws()
    out = json.loads(_run(vi_mod.vision_interpret(
        {"image_path": "a.png", "question": "图里有什么？", "detail": "high"},
        workspace_path=ws)))
    assert out.get("success") is True, out
    assert out["interpretation"] == "图中是一只猫。"
    parts = captured["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "图里有什么？"}
    assert parts[1]["type"] == "image_url"
    url = parts[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,"), url[:60]
    assert parts[1]["image_url"].get("detail") == "high"


def test_symlink_escape_rejected():
    from app.tools.vision_interpret import vision_interpret
    ws = _make_ws()
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as outside:
        outside.write(_TINY_PNG)
    os.symlink(outside.name, os.path.join(ws, "linked.png"))
    out = json.loads(_run(vision_interpret(
        {"image_path": "linked.png", "question": "q"}, workspace_path=ws)))
    assert out.get("success") is False, "symlink resolving outside the workspace must be rejected"


def test_oversize_rejected(monkeypatch):
    from app.tools import vision_interpret as vi_mod
    ep = ModelEndpoint(alias="vlm", kind=KIND_LLM, base_url="http://vlm.local/v1",
                       api_key="sk-x", model_name="qwen-vl-max")
    # 2026-11-01：解析顺序改为主模型探针优先——fixture 必须同时给出主端点
    # （此处主端点为空 = 无视觉），才能测到 vlm 回落路径。
    _empty_main = ModelEndpoint(alias="main", kind=KIND_LLM, base_url="", api_key="",
                                model_name="")
    fake_registry = type("R", (), {
        "resolve": lambda self, purpose: (_empty_main if purpose == "main" else ep),
        "endpoint_for_assistant": lambda self, assistant: _empty_main,
    })()
    monkeypatch.setattr("app.model_gateway.registry.get_model_registry", lambda: fake_registry)
    monkeypatch.setattr(vi_mod, "_MAX_IMAGE_BYTES", 10)  # a.png is 68 bytes
    ws = _make_ws()
    out = json.loads(_run(vi_mod.vision_interpret(
        {"image_path": "a.png", "question": "q"}, workspace_path=ws)))
    assert out.get("success") is False and "过大" in out["error"]


def test_empty_key_never_falls_back_to_main_key(monkeypatch):
    """A4.9 W2-I1 (Security): a keyless vlm endpoint must wire 'no-key', not
    the main provider key."""
    from app.tools import vision_interpret as vi_mod
    ep = ModelEndpoint(alias="vlm", kind=KIND_LLM, base_url="http://vlm.local/v1",
                       api_key="", model_name="qwen-vl-max")
    # 2026-11-01：解析顺序改为主模型探针优先——fixture 必须同时给出主端点
    # （此处主端点为空 = 无视觉），才能测到 vlm 回落路径。
    _empty_main = ModelEndpoint(alias="main", kind=KIND_LLM, base_url="", api_key="",
                                model_name="")
    fake_registry = type("R", (), {
        "resolve": lambda self, purpose: (_empty_main if purpose == "main" else ep),
        "endpoint_for_assistant": lambda self, assistant: _empty_main,
    })()
    monkeypatch.setattr("app.model_gateway.registry.get_model_registry", lambda: fake_registry)
    captured = {}

    class _FakeLLM:
        async def stream_chat_structured(self, messages, **kwargs):
            yield {"type": "content", "data": "ok"}
            yield {"type": "done", "data": {"finish_reason": "stop"}}

    def _fake_build(ep_, **k):
        captured["api_key"] = ep_.api_key
        return _FakeLLM()

    monkeypatch.setattr("app.model_gateway.factory.build_llm_service", _fake_build)
    ws = _make_ws()
    out = json.loads(_run(vi_mod.vision_interpret(
        {"image_path": "a.png", "question": "q"}, workspace_path=ws)))
    assert out.get("success") is True
    assert captured["api_key"] == "no-key", captured


# ── Criterion 24: canonical vlm alias + legacy compat ────────────────────

def test_vlm_canonical_alias_from_vlm_section():
    conf = {"vlm": {"base_url": "http://vlm.local/v1", "api_key": "sk-x", "model_name": "qwen-vl-max"}}
    endpoints, routing = synthesize(conf)
    assert "vlm" in endpoints, "canonical vlm endpoint must be synthesized"
    assert endpoints["vlm"].base_url == "http://vlm.local/v1"
    assert routing["vlm"] == "vlm"


def test_vlm_canonical_alias_from_legacy_deathmatch_section():
    conf = {"deathmatch": {"vlm": {"base_url": "http://legacy/v1", "api_key": "sk-l", "model_name": "mm-1"}}}
    endpoints, routing = synthesize(conf)
    assert endpoints["vlm"].base_url == "http://legacy/v1", (
        "legacy [deathmatch.vlm] config must populate the canonical vlm endpoint"
    )
    assert routing["vlm"] == "vlm"


def test_vlm_hidden_from_public_aliases():
    conf = {"vlm": {"base_url": "http://vlm.local/v1", "api_key": "sk-x", "model_name": "qwen-vl-max"}}
    reg = ModelRegistry.from_sources(conf)
    public = {a["alias"] for a in reg.public_aliases()}
    assert "vlm" not in public and "deathmatch.vlm" not in public, (
        "vision endpoints must not leak into the assistant model dropdown"
    )


def test_vlm_legacy_toml_endpoint_compat():
    """A deployment that configured [endpoints.\"deathmatch.vlm\"] explicitly
    (pre-canonical) keeps working via the PRODUCTION from_sources path
    (merged config after the 2026-08-31 consolidation): purpose vlm resolves
    to the legacy endpoint when canonical is empty."""
    conf = {"endpoints": {"deathmatch.vlm": {"kind": "llm", "base_url": "http://legacy/v1",
                                             "api_key": "sk-l", "model_name": "mm-1"}}}
    reg = ModelRegistry.from_sources(conf)
    ep = reg.resolve("vlm")
    assert ep.alias == "deathmatch.vlm" and ep.base_url == "http://legacy/v1"


def test_provider_names_excludes_internal_aliases():
    """A4.9 W2-M2: a configured vision endpoint must never become a chat
    fallback candidate."""
    conf = {"vlm": {"base_url": "http://vlm.local/v1", "api_key": "sk-x", "model_name": "qwen-vl-max"}}
    reg = ModelRegistry.from_sources(conf)
    names = reg.provider_names()
    assert "vlm" not in names and "deathmatch.vlm" not in names


def test_assistant_alias_rejects_internal_purpose():
    """A4.9 W2-M3: model_alias='vlm' must fail assistant validation."""
    from pydantic import ValidationError
    from app.schemas.assistant import AssistantCreate
    with pytest.raises(ValidationError):
        AssistantCreate(name="x", model_alias="vlm")
