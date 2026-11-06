"""P0 R1 prompt: the citation ledger must never be reproduced as an answer tail."""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.prompts.system_agent import build_agent_system_prompt


def test_prompt_bans_manifest_echo_and_points_to_citation_cards():
    text = asyncio.run(build_agent_system_prompt(
        assistant=SimpleNamespace(system_prompt="基准助手", name="基准助手"),
        shared_context=SimpleNamespace(memory_summary="", dream_summary="", memory_entries=[]),
        workspace=SimpleNamespace(root_path="/tmp/prompt-baseline"),
        user=SimpleNamespace(id=None),
        identity_context="",
        conversation_id="",
        deathmatch_mode=False,
        offered_tool_names=["web_search"],
        task="research",
    ))
    text = re.sub(r"当前时间: .*", "当前时间: <normalized>", text)
    assert "禁止逐条复述" in text
    assert "[N] 标题" in text or "[N]标题" in text
    assert "citation" in text.lower() or "引用卡片" in text
