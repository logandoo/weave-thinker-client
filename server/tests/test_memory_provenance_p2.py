"""P2 TMA-lite: memory entry signatures detect tampering; legacy is unsigned."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import json
from types import SimpleNamespace

from app.services.memory_provenance import (
    annotate_entries, sign_entry, sync_entries, verify_entry,
)
from app.tools.memory import memory


def test_sign_verify_and_tamper_detection():
    sig = sign_entry("u1", "user", "likes AMD")
    assert verify_entry("u1", "user", "likes AMD", sig)
    assert not verify_entry("u1", "user", "likes Intel", sig)
    assert not verify_entry("u2", "user", "likes AMD", sig)


def test_annotate_entries_marks_signed_and_legacy(tmp_path):
    path = tmp_path / "USER.md"
    path.write_text("a\n§\nb\n", encoding="utf-8")
    sync_entries(path, "u1", "user", ["a", "b"], origins=["agent", "agent"])
    meta = annotate_entries(path, "u1", "user", ["a", "legacy"])
    assert meta[0]["verified"] is True
    assert meta[0]["status"] == "signed"
    assert meta[0]["origin"] == "agent"
    assert meta[1]["verified"] is False
    assert meta[1]["status"] == "legacy-unsigned"
    assert meta[1]["origin"] == "legacy_untrusted"
    # Sidecar exists but the content was tampered -> not merely legacy.
    import json
    from app.services.memory_provenance import content_hash, load_map, sidecar_path
    path.write_text("tampered\n§\n", encoding="utf-8")
    raw = load_map(sidecar_path(path))
    raw[content_hash("tampered")] = sign_entry("u1", "user", "a")
    sidecar_path(path).write_text(json.dumps(raw), encoding="utf-8")
    meta2 = annotate_entries(path, "u1", "user", ["tampered"])
    assert meta2[0]["verified"] is False
    assert meta2[0]["status"] == "tampered"
    assert meta2[0]["origin"] == "untrusted_external"


def test_memory_tool_read_reports_verified_and_tampered_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path))
    user = SimpleNamespace(id="u-int")
    added = json.loads(asyncio.run(memory(
        {"action": "add", "target": "agent", "content": "signed entry"}, user=user,
    )))
    assert added["count"] == 1
    read = json.loads(asyncio.run(memory(
        {"action": "read", "target": "agent"}, user=user,
    )))
    assert read["entries_meta"][0]["verified"] is True
    path = tmp_path / "u-int" / "AGENT.md"
    path.write_text("tampered\n§\n", encoding="utf-8")
    read2 = json.loads(asyncio.run(memory(
        {"action": "read", "target": "agent"}, user=user,
    )))
    assert read2["entries_meta"][0]["verified"] is False
