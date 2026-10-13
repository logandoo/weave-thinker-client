"""RED tests (A4.8) — 审计独立模型路由（[routing] "agent.audit"），conv 174182bc。

根因：预发送审计与写入同模型（qwen3.8-flash-next，4bit 本地）——零引用稿
（cited=0, ledger=18/20/10）被 accept 两次。修复=显式 operator 路由 purpose
`agent.audit`（config_model.toml [routing]，缺省不配置=继承 coordinator/主模型，
P0 助手全链路继承 feedback 2026-08-21 语义不变）。

修复契约：
  R1 `_resolve_audit_llm()`：未显式配置 → None；显式配置 → 按该端点构建 LLMService。
  R2 `AgentLoop.audit_llm` 优先级：显式传入 > [routing] 解析 > coordinator_llm。
  R3 `_complete_json(..., llm=...)` 把调用路由到指定客户端（缺省 coordinator_llm）。
  R4 `_audit_response` 的审计 JSON 调用带 `llm=self.audit_llm`。

Run: cd backend && python -m pytest tests/test_audit_model_routing.py -v
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.agent_loop as agent_loop_mod
from app.model_gateway.schemas import ModelEndpoint
from app.services.agent_loop import AgentLoop, AgentLoopState


class _FakeRegistry:
    def __init__(self, explicit=False, endpoint=None, target=None, known=True):
        self._explicit = explicit
        self._endpoint = endpoint
        self._target = target if target is not None else "deepseek"
        self._known = known

    def has_explicit_routing(self, purpose):
        return self._explicit and purpose == "agent.audit"

    def routing_target(self, purpose):
        assert purpose == "agent.audit"
        return self._target

    def has(self, alias):
        return bool(self._known)

    def resolve(self, purpose):
        assert purpose == "agent.audit"
        return self._endpoint


class _FakeLLM:
    def __init__(self, tag):
        self.tag = tag
        self.calls = []
        self.kwargs = []
        self.endpoint = ModelEndpoint(alias=f"fake-{tag}", kind="llm", provider_type="deepseek")
        self.provider_type = "deepseek"

    async def complete_chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        self.kwargs.append(dict(kwargs))
        return '{"ok": true, "problem": ""}'


class _RejectingLLM(_FakeLLM):
    """模拟不支持 response_format 的端点：带该参数即报错。"""

    async def complete_chat(self, messages, **kwargs):
        if "response_format" in kwargs:
            raise RuntimeError("Unrecognized request argument supplied: response_format")
        self.calls.append(list(messages))
        self.kwargs.append(dict(kwargs))
        return '{"ok": true, "problem": ""}'


def _patch_registry(monkeypatch, reg):
    monkeypatch.setattr(
        "app.model_gateway.registry.get_model_registry", lambda: reg
    )


# ── R1 解析器 ─────────────────────────────────────────────────────────────

def test_resolve_audit_llm_none_without_explicit_routing(monkeypatch):
    _patch_registry(monkeypatch, _FakeRegistry(explicit=False))
    assert agent_loop_mod._resolve_audit_llm() is None


def test_resolve_audit_llm_builds_from_explicit_routing(monkeypatch):
    ep = ModelEndpoint(
        alias="deepseek", kind="llm", base_url="https://api.deepseek.com/v1",
        api_key="test-key", model_name="deepseek-flash", provider_type="deepseek",
        is_custom=True,
    )
    _patch_registry(monkeypatch, _FakeRegistry(explicit=True, endpoint=ep))
    llm = agent_loop_mod._resolve_audit_llm()
    assert llm is not None
    assert getattr(llm, "endpoint", None) is not None
    assert llm.endpoint.alias == "deepseek"


def test_resolve_audit_llm_failsafe_on_error(monkeypatch):
    class _Boom:
        def has_explicit_routing(self, purpose):
            raise RuntimeError("registry broken")

    _patch_registry(monkeypatch, _Boom())
    assert agent_loop_mod._resolve_audit_llm() is None  # fail-safe 继承


# ── A4.9 R2 Minor 收口：缓存 + 误配告警 ───────────────────────────────────

def test_resolve_audit_llm_caches_client(monkeypatch):
    monkeypatch.setattr(agent_loop_mod, "_AUDIT_LLM_CACHE", {})
    ep = ModelEndpoint(
        alias="deepseek", kind="llm", base_url="https://api.deepseek.com/v1",
        api_key="test-key", model_name="deepseek-flash", provider_type="deepseek",
        is_custom=True,
    )
    _patch_registry(monkeypatch, _FakeRegistry(explicit=True, endpoint=ep))
    a = agent_loop_mod._resolve_audit_llm()
    b = agent_loop_mod._resolve_audit_llm()
    assert a is not None and a is b, "同一端点配置必须复用缓存的审计客户端"


def test_resolve_audit_llm_warns_on_unknown_alias(monkeypatch, caplog):
    monkeypatch.setattr(agent_loop_mod, "_AUDIT_LLM_CACHE", {})
    ep = ModelEndpoint(alias="main", kind="llm", model_name="m", provider_type="deepseek")
    _patch_registry(monkeypatch, _FakeRegistry(
        explicit=True, endpoint=ep, target="deepsek", known=False,
    ))
    with caplog.at_level("WARNING", logger="app.services.agent_loop"):
        llm = agent_loop_mod._resolve_audit_llm()
    assert llm is not None
    assert any("not in registry" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


# ── R2 优先级 ─────────────────────────────────────────────────────────────

def test_init_prefers_explicit_audit_llm():
    sentinel = _FakeLLM("passed")
    loop = AgentLoop(llm=object(), tool_schemas=[], audit_llm=sentinel)
    assert loop.audit_llm is sentinel


def test_init_resolves_routing_when_not_passed(monkeypatch):
    sentinel = _FakeLLM("resolved")
    monkeypatch.setattr(agent_loop_mod, "_resolve_audit_llm", lambda: sentinel)
    loop = AgentLoop(llm=object(), tool_schemas=[])
    assert loop.audit_llm is sentinel


def test_init_inherits_coordinator_when_unrouted(monkeypatch):
    monkeypatch.setattr(agent_loop_mod, "_resolve_audit_llm", lambda: None)
    coord = _FakeLLM("coord")
    loop = AgentLoop(llm=object(), tool_schemas=[], coordinator_llm=coord)
    assert loop.audit_llm is coord


# ── R3 _complete_json 路由 ────────────────────────────────────────────────

def _loop_for_json(coord, audit):
    loop = AgentLoop.__new__(AgentLoop)
    loop.provider_type = "qwen3.8_vllm"
    loop.thinking_budget = None
    loop.preserve_thinking = False
    loop._json_mode_disabled = set()
    loop.coordinator_llm = coord
    loop.audit_llm = audit
    return loop


def test_complete_json_routes_to_given_llm():
    coord, audit = _FakeLLM("coord"), _FakeLLM("audit")
    loop = _loop_for_json(coord, audit)
    out = asyncio.run(loop._complete_json(
        [{"role": "user", "content": "x"}], temperature=0.0, llm=loop.audit_llm,
    ))
    assert out == '{"ok": true, "problem": ""}'
    assert audit.calls and not coord.calls, "审计 JSON 调用必须路由到 audit_llm"


def test_complete_json_defaults_to_coordinator():
    coord, audit = _FakeLLM("coord"), _FakeLLM("audit")
    loop = _loop_for_json(coord, audit)
    asyncio.run(loop._complete_json([{"role": "user", "content": "x"}], temperature=0.3))
    assert coord.calls and not audit.calls, "缺省必须维持 coordinator 路由（P0 继承）"


def test_json_mode_rejection_is_per_client():
    """A4.9 R2 Minor：审计端点拒绝 response_format 只降级该客户端，
    coordinator 的 JSON 模式不受牵连。"""
    coord, audit = _FakeLLM("coord"), _RejectingLLM("audit")
    loop = _loop_for_json(coord, audit)
    out = asyncio.run(loop._complete_json(
        [{"role": "user", "content": "x"}], temperature=0.0, llm=loop.audit_llm,
    ))
    assert out == '{"ok": true, "problem": ""}'  # 降级重试成功
    assert id(audit) in loop._json_mode_disabled
    assert id(coord) not in loop._json_mode_disabled
    # coordinator 仍带 response_format
    asyncio.run(loop._complete_json([{"role": "user", "content": "x"}], temperature=0.3))
    assert "response_format" in coord.kwargs[-1], coord.kwargs[-1]


# ── R4 _audit_response 透传（harness 同 test_audit_citation_mapping） ──────

def test_audit_response_passes_audit_llm():
    loop = AgentLoop.__new__(AgentLoop)
    loop.llm = None
    loop.iteration_llm = None
    loop.provider_type = "qwen3.8_vllm"
    loop.enable_reasoning = False
    loop.reasoning_effort = None
    loop.thinking_budget = None
    loop.preserve_thinking = False
    loop.deathmatch_manager = None
    loop._compressor = None
    loop.enable_compression = False
    loop._canary_enabled = False
    loop._canary_marker = None
    loop.max_iterations = 30
    loop.tool_schemas = []
    loop._active_tool_names = set()
    loop.session_factory = None
    loop._tool_call_timeout = 60
    loop.workspace_path = None
    loop._try_fallback_provider = lambda: False
    loop.audit_llm = _FakeLLM("audit")

    captured = {}

    class _Recorder:
        async def __call__(self, messages, temperature=None, max_tokens=None, **kw):
            captured.update(kw)
            return '{"ok": true, "problem": ""}'

    loop._complete_json = _Recorder()
    state = AgentLoopState(messages=[{"role": "user", "content": "你好"}])
    verdict = asyncio.run(loop._audit_response(state, "一段普通草稿，没有引用。", "你好"))
    assert verdict is None
    assert captured.get("llm") is loop.audit_llm
