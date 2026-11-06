"""C-05: cache/prompt-meta sampling report + context persistence."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.prompt_cache_report import _report


def _row(**info):
    return json.dumps(info)


def test_report_separates_legacy_rows_and_cache_coverage():
    rows = [
        _row(tokens=100, context_length=1000, measured=True),  # legacy: no meta
        _row(prompt_id="agent.system", prompt_variant="full", tokens=200,
             prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20),
        _row(prompt_id="agent.system", prompt_variant="full", tokens=300),
        _row(prompt_id="audit.response", prompt_variant="full", tokens=50,
             cache_read_input_tokens=40),
    ]
    data = _report(rows)

    assert data["messages_with_context"] == 4
    assert data["legacy_messages"] == 1
    assert data["cache_counted_messages"] == 2

    groups = {g["key"]: g for g in data["groups"]}
    agent = groups["agent.system:full"]
    assert agent["messages"] == 2
    assert agent["cache_counted"] == 1
    assert agent["hit_rate"] == 0.8
    assert groups["audit.response:full"]["cache_read"] == 40
    assert groups["?:?"]["messages"] == 1


def test_report_marks_uncounted_group_rate_as_none():
    data = _report([_row(prompt_id="x", prompt_variant="full", tokens=1)])
    group = data["groups"][0]
    assert group["cache_counted"] == 0
    assert group["hit_rate"] is None


def test_context_info_preserves_prompt_and_cache_fields():
    from app.services.agent_loop_context import _context_info_from_usage

    info = _context_info_from_usage({
        "prompt_tokens": 900,
        "prompt_id": "agent.system",
        "prompt_version": "1.1.0",
        "prompt_hash": "abc123",
        "prompt_release_hash": "full-hash",
        "prompt_id_source": "explicit",
        "system_tokens": 700,
        "tools_tokens": 100,
        "cache_read_input_tokens": 512,
        "prompt_cache_hit_tokens": 512,
        "prompt_cache_miss_tokens": 388,
        "model_alias": "deepseek-flash",
        "model_name": "deepseek-flash-1017",
    }, 128000)
    assert info["tokens"] == 900
    assert info["prompt_id"] == "agent.system"
    assert info["prompt_version"] == "1.1.0"
    assert info["prompt_id_source"] == "explicit"
    assert info["prompt_release_hash"] == "full-hash"
    assert info["cache_read_input_tokens"] == 512
    assert info["prompt_cache_hit_tokens"] == 512
    assert info["model_alias"] == "deepseek-flash"
    assert info["model_name"] == "deepseek-flash-1017"
