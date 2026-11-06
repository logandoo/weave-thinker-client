"""C-13: prompt attribution is explicit, and the release hash is complete.

The old heuristic silently labelled every unknown system message
``agent.system`` and hashed only the system text, so two different wire
releases (different tools schema / model / sampling params) shared a hash.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.prompts.observability import prompt_meta


TOOLS = [{"type": "function", "function": {"name": "web_search", "parameters": {}}}]


def test_unknown_system_message_is_not_silently_agent_system():
    meta = prompt_meta([{"role": "system", "content": "totally custom"}], [], "deepseek")
    assert meta["prompt_id"] == "unclassified"
    assert meta["prompt_id_source"] == "fallback"


def test_explicit_prompt_id_wins_over_marker():
    messages = [{"role": "system", "content": "你是一个智能路由协调器"}]
    meta = prompt_meta(messages, [], "deepseek", prompt_id="agent.system")
    assert meta["prompt_id"] == "agent.system"
    assert meta["prompt_id_source"] == "explicit"


def test_message_level_prompt_id_is_honoured():
    messages = [{"role": "system", "content": "anything", "prompt_id": "title.generation"}]
    meta = prompt_meta(messages, [], "deepseek")
    assert meta["prompt_id"] == "title.generation"
    assert meta["prompt_id_source"] == "message"


def test_marker_inference_is_labelled():
    meta = prompt_meta([{"role": "system", "content": "你是一个智能路由协调器"}], [], "deepseek")
    assert meta["prompt_id"] == "coordinator.route"
    assert meta["prompt_id_source"] == "inferred"


def test_release_hash_covers_tools_model_and_params():
    messages = [{"role": "system", "content": "rules"}]
    base = prompt_meta(messages, TOOLS, "deepseek", params={"temperature": 0.7})
    same = prompt_meta(messages, TOOLS, "deepseek", params={"temperature": 0.7})
    assert base["prompt_release_hash"] == same["prompt_release_hash"]

    variants = [
        prompt_meta(messages, TOOLS + [{"type": "function", "function": {"name": "x"}}],
                    "deepseek", params={"temperature": 0.7}),
        prompt_meta(messages, TOOLS, "mimo", params={"temperature": 0.7}),
        prompt_meta(messages, TOOLS, "deepseek", params={"temperature": 0.0}),
        prompt_meta(messages, TOOLS, "deepseek", params={"temperature": 0.7},
                    provider_type="anthropic"),
        prompt_meta(messages, TOOLS, "deepseek",
                    params={"temperature": 0.7, "tool_spotlight": False}),
    ]
    hashes = {base["prompt_release_hash"], *(v["prompt_release_hash"] for v in variants)}
    assert len(hashes) == 6, "each release factor must change the hash"
    # the short system-only hash intentionally stays stable
    assert variants[0]["prompt_hash"] == base["prompt_hash"]


def test_build_params_attaches_explicit_prompt_id():
    from app.model_gateway import factory
    from app.model_gateway.schemas import KIND_LLM, ModelEndpoint

    ep = ModelEndpoint(
        alias="deepseek", kind=KIND_LLM,
        base_url="https://api.deepseek.com/v1", api_key="test-key",
        model_name="deepseek-flash", provider_type="deepseek",
        is_custom=True, params={},
    )
    llm = factory.build_llm_service(ep)
    llm._build_params(
        [{"role": "system", "content": "main"},
         {"role": "user", "content": "hi"}],
        tools=TOOLS, prompt_id="agent.system",
    )
    meta = llm._last_prompt_meta
    assert meta["prompt_id"] == "agent.system"
    assert meta["prompt_id_source"] == "explicit"
    assert meta["model_alias"] == "deepseek"
    assert meta["model_name"] == "deepseek-flash"
