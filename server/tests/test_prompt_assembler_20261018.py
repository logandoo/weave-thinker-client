"""C-12: the agent system prompt is assembled from PromptSections.

The byte-level baseline was captured BEFORE the assembler refactor
(2026-10-18).  Any reordering/tier change must show up here, not in a
production prompt diff.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.prompts.system_agent import build_agent_system_prompt

BASELINE = Path(__file__).with_name("prompt_evals") / "agent_system_baseline_20261018.txt"


def _build() -> str:
    text = asyncio.run(build_agent_system_prompt(
        assistant=SimpleNamespace(system_prompt="Persona 测试段。", name="基准助手"),
        shared_context=SimpleNamespace(
            memory_summary="记忆摘要 A", dream_summary="梦境 B", memory_entries=[]),
        workspace=SimpleNamespace(root_path="/tmp/prompt-baseline"),
        user=SimpleNamespace(id=None),
        identity_context="",
        conversation_id="",
        deathmatch_mode=False,
        offered_tool_names=["web_search"],
        task="research",
    ))
    return re.sub(r"当前时间: .*", "当前时间: <normalized>", text)


def test_assembler_output_is_byte_identical_to_pre_refactor_baseline():
    assert _build() == BASELINE.read_text(encoding="utf-8")


def test_stable_prefix_does_not_depend_on_volatile_memory():
    a = _build()
    b = asyncio.run(build_agent_system_prompt(
        assistant=SimpleNamespace(system_prompt="Persona 测试段。", name="基准助手"),
        shared_context=SimpleNamespace(
            memory_summary="完全不同的记忆", dream_summary="另一个梦", memory_entries=[]),
        workspace=SimpleNamespace(root_path="/tmp/prompt-baseline"),
        user=SimpleNamespace(id=None),
        identity_context="",
        conversation_id="",
        deathmatch_mode=False,
        offered_tool_names=["web_search"],
        task="research",
    ))
    b = re.sub(r"当前时间: .*", "当前时间: <normalized>", b)
    marker = "## 运行时状态"
    assert a.split(marker)[0] == b.split(marker)[0]
    assert "完全不同的记忆" in b
