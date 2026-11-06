"""P4 token 估算修正单测：include_usage 真值流 + 回滚模式剔除 search_tools
+ search_tools 的 blocked 过滤。

LLMService 用 __new__ 跳过构造（零网络），手工注入 config 与假 client。
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from app.core.config import get_config  # noqa: E402
from app.services.llm_service import LLMService  # noqa: E402
from app.services.agent_loop import AgentLoop, _context_info_from_usage  # noqa: E402
from app.tools import registry  # noqa: E402
from app.tools.mcp_search import search_tools_handler  # noqa: E402

config = get_config()


def _chunk(content=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=None)],
        usage=usage,
    )


def _usage_obj(**kw):
    base = dict(prompt_tokens=1234, completion_tokens=56,
                prompt_cache_hit_tokens=1000, prompt_cache_miss_tokens=234)
    base.update(kw)
    return SimpleNamespace(**base)


class FakeCompletions:
    def __init__(self, chunks, fail_first_msg=None):
        self.chunks = chunks
        self.calls = []
        self.fail_first_msg = fail_first_msg

    async def create(self, **params):
        self.calls.append(params)
        if self.fail_first_msg and len(self.calls) == 1:
            raise Exception(self.fail_first_msg)

        async def _gen():
            for c in self.chunks:
                yield c
        return _gen()


def _svc(chunks, fail_first_msg=None):
    svc = LLMService.__new__(LLMService)
    svc.config = config
    svc.custom_model_name = None
    svc.endpoint = None
    svc.is_custom_provider = False
    svc.preserve_reasoning = False
    svc.client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions(chunks, fail_first_msg)))
    return svc


async def _collect(svc):
    return [e async for e in svc.stream_chat_structured(
        [{"role": "user", "content": "hi"}])]


def test_stream_yields_usage_event():
    svc = _svc([_chunk(content="hi"), _chunk(usage=_usage_obj())])
    events = asyncio.run(_collect(svc))
    usage_events = [e for e in events if e["type"] == "usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["data"]["prompt_tokens"] == 1234
    assert usage_events[0]["data"]["prompt_cache_hit_tokens"] == 1000
    assert any(e["type"] == "content" and e["data"] == "hi" for e in events)
    assert events[-1]["type"] == "done"


def test_stream_options_sent_when_enabled():
    svc = _svc([_chunk(content="x")])
    asyncio.run(_collect(svc))
    params = svc.client.chat.completions.calls[0]
    assert params.get("stream_options") == {"include_usage": True}


def test_stream_options_absent_when_disabled():
    cls = type(config)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(cls, "llm_stream_include_usage", property(lambda self: False))
    try:
        svc = _svc([_chunk(content="x")])
        asyncio.run(_collect(svc))
        params = svc.client.chat.completions.calls[0]
        assert "stream_options" not in params
    finally:
        monkey.undo()


def test_retry_without_stream_options_on_unsupported():
    svc = _svc([_chunk(content="ok")],
               fail_first_msg="Unrecognized request argument: stream_options")
    events = asyncio.run(_collect(svc))
    calls = svc.client.chat.completions.calls
    assert len(calls) == 2
    assert "stream_options" in calls[0]
    assert "stream_options" not in calls[1]
    assert any(e["type"] == "content" and e["data"] == "ok" for e in events)
    assert not any(e["type"] == "error" for e in events)


def test_context_info_from_usage_helper():
    info = _context_info_from_usage({"prompt_tokens": 4321}, 262144)
    assert info == {"tokens": 4321, "context_length": 262144, "measured": True}


# ---- 回滚模式剔除 search_tools（A4.9 R1 Minor-1 收口） ----

def test_rollback_mode_strips_search_tools(monkeypatch):
    monkeypatch.setitem(config.agent, "tools_catalog_discovery_enabled", False)
    cls = type(config)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(cls, "agent_tools_mcp_progressive_enabled",
                   property(lambda self: False))
    try:
        loop = AgentLoop(llm=object())
        names = [(s.get("function") or {}).get("name") for s in loop.tool_schemas]
        assert "search_tools" not in names
        assert "web_search" in names  # 其余工具照旧
    finally:
        monkey.undo()


# ---- search_tools blocked 过滤（A4.9 R1 Minor-4 收口） ----

def test_search_tools_handler_filters_blocked(monkeypatch):
    monkeypatch.setitem(config.agent, "tools_catalog_discovery_enabled", False)
    name = "mcp_progtest_blocked_probe"
    registry.register(
        name=name, toolset="mcp-progtest",
        schema={"name": name, "description": "blocked probe tool",
                "parameters": {"type": "object"}},
        handler=lambda args, **kw: "{}", is_async=False, description="blocked probe",
    )
    try:
        out = json.loads(asyncio.run(search_tools_handler(
            {"query": "blocked probe"}, _blocked_tool_names={name})))
        # 2026-11-01: a configured MCP server (weave-connector) can legitimately
        # match the same query words ("blocked/confirmed" in its description).
        # The contract under test is that the BLOCKED tool is never advertised,
        # not that the catalog is globally empty.
        assert not any(m["name"] == name for m in out["matches"])
        assert all(m["name"] != name for m in out["matches"])
        out2 = json.loads(asyncio.run(search_tools_handler({"query": "blocked probe"})))
        assert any(m["name"] == name for m in out2["matches"])
    finally:
        registry.unregister(name)


def test_rollback_dispatch_rejects_search_tools(monkeypatch):
    """A4.9 R1 M4：回滚模式下幻觉调用 search_tools → 明确拒绝（非目录检索）。"""
    monkeypatch.setitem(config.agent, "tools_catalog_discovery_enabled", False)
    from app.services.agent_loop import AgentLoopState
    cls = type(config)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(cls, "agent_tools_mcp_progressive_enabled",
                   property(lambda self: False))
    try:
        loop = AgentLoop(llm=object())
        result = asyncio.run(loop._execute_single_tool(
            "c1", "search_tools", {}, None, None, None, None, AgentLoopState()))
        assert result.error
        assert "search_tools 当前不可用" in result.result
    finally:
        monkey.undo()


def test_null_usage_no_event():
    """遗留①：null-usage 供应商不产生 usage 事件（防止 measured tokens:0 徽章）。"""
    svc = _svc([_chunk(content="x"),
                _chunk(usage=_usage_obj(prompt_tokens=None, completion_tokens=None))])
    events = asyncio.run(_collect(svc))
    assert not [e for e in events if e["type"] == "usage"]


def test_retry_failure_combines_errors():
    """遗留④：降级重试仍失败时，错误信息同时携带首错与重试错。"""
    svc = _svc([], fail_first_msg="Unrecognized request argument: stream_options")
    svc.client.chat.completions.fail_second_msg = None

    calls = svc.client.chat.completions

    async def create(**params):
        calls.calls.append(params)
        if len(calls.calls) == 1:
            raise Exception("Unrecognized request argument: stream_options")
        raise Exception("backend went away after retry")
    calls.create = create

    events = asyncio.run(_collect(svc))
    errs = [e for e in events if e["type"] == "error"]
    assert len(errs) == 1
    assert "stream_options" in errs[0]["data"]
    assert "went away after retry" in errs[0]["data"]
