"""P2 S4/APM: empty memory reads must declare missing evidence."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools.memory import memory


def test_empty_memory_read_declares_missing_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path))
    payload = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"},
        user=SimpleNamespace(id="u-apm"),
    )))
    assert payload["evidence_status"] == "missing"
    assert payload["missing_evidence"] is True
    assert payload["count"] == 0
    assert "不要把它当作已找到证据" in payload["note"]
