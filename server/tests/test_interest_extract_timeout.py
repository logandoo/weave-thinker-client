"""interest_extract 超时配置化（[memory] interest_extract_timeout_seconds，默认 120——
2026-09-10 超时审核 wave：LLM 判断类超时不应过短，用户裁决由 40 上调至 120）。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.core.config import get_config
from app.services.proactive_learning_service import ProactiveLearningService

config = get_config()


def _capturing_judge(captured, response=None):
    async def _stub(system_prompt, user_prompt, **kwargs):
        captured.update(kwargs)
        return response or {"topics": {"x": 1}, "preferences": {"response_style": "balanced"}}
    return _stub


def _run(captured, monkey):
    monkey.setattr("app.services.agentic_judge.judge_json", _capturing_judge(captured))
    return asyncio.run(ProactiveLearningService().extract_interests(
        [{"role": "user", "content": "hello world"}]))


def test_default_timeout_is_120():
    assert config.memory_interest_extract_timeout_seconds == 120.0


def test_extract_uses_configured_timeout_default():
    captured = {}
    monkey = pytest.MonkeyPatch()
    try:
        out = _run(captured, monkey)
        assert captured["timeout"] == 120.0
        assert out["topics"] == {"x": 1}
    finally:
        monkey.undo()


def test_extract_timeout_follows_config_override():
    captured = {}
    monkey = pytest.MonkeyPatch()
    monkey.setattr(type(config), "memory_interest_extract_timeout_seconds",
                   property(lambda self: 55.0))
    try:
        _run(captured, monkey)
        assert captured["timeout"] == 55.0
    finally:
        monkey.undo()


def test_timeout_failure_falls_back_to_structural():
    async def _timeout_stub(system_prompt, user_prompt, **kwargs):
        return None
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr("app.services.agentic_judge.judge_json", _timeout_stub)
        out = asyncio.run(ProactiveLearningService().extract_interests(
            [{"role": "user", "content": "hello"}]))
        assert out["topics"] == {} and "interaction_patterns" in out
    finally:
        monkey.undo()
