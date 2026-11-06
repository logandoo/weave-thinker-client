"""P1 I3: prompt_variant must survive usage -> context_info -> cache report."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.agent_loop_context import _context_info_from_usage


def test_prompt_variant_is_persisted_in_context_info():
    info = _context_info_from_usage(
        {"prompt_tokens": 123, "prompt_variant": "compact", "prompt_id": "agent.system"},
        context_window=262144,
    )
    assert info["prompt_variant"] == "compact"
    assert info["measured"] is True
