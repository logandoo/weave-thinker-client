"""P0 TMA-NM: file-memory authority is origin/tenant bound (advisory, not full IFC).

Covers plan §5 P0: v2 HMAC canonicalisation, authority metadata, legacy
classification, cross-scope reuse, atomic sidecar writes, no origin
laundering on rewrite, strict quarantine, and the tenant filesystem guard.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.memory_provenance as mp
from app.services.tenant_service import TenantConfigError, tenant_config_errors
from app.tools.memory import memory


DELIM = "\n§\n"


def _env(tmp_path, monkeypatch, *, tenant=None, experimental=None):
    monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path))
    if tenant is None:
        monkeypatch.delenv("WT_TENANT_ID", raising=False)
    else:
        monkeypatch.setenv("WT_TENANT_ID", tenant)
    if experimental is None:
        monkeypatch.delenv("WT_MULTI_TENANT_EXPERIMENTAL", raising=False)
    else:
        monkeypatch.setenv("WT_MULTI_TENANT_EXPERIMENTAL", "true" if experimental else "false")


def _write(path: Path, entries: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DELIM.join(entries) + DELIM if entries else "", encoding="utf-8")


def _ready_key(monkeypatch) -> None:
    from app.core.config import get_config
    monkeypatch.setitem(get_config().security, "memory_hmac_key", "k" * 32)


def _legacy_signature(user_id: str, target: str, content: str) -> str:
    """The pre-P0 signature shape: user\x00target\x00content_hash (no tenant/origin)."""
    msg = f"{user_id}\x00{target}\x00{mp.content_hash(content)}".encode("utf-8")
    return hmac.new(mp._secret(), msg, hashlib.sha256).hexdigest()


def test_signature_binds_origin_and_tenant():
    sig = mp.sign_entry("u1", "user", "likes AMD", origin="agent", tenant="acme")
    assert mp.verify_entry("u1", "user", "likes AMD", sig, origin="agent", tenant="acme")
    # An attacker cannot raise authority by relabelling the same content.
    assert not mp.verify_entry("u1", "user", "likes AMD", sig, origin="user", tenant="acme")
    # A sidecar copied to another tenant does not verify.
    assert not mp.verify_entry("u1", "user", "likes AMD", sig, origin="agent", tenant="beta")
    assert not mp.verify_entry("u1", "user", "likes AMD", sig, origin="agent", tenant="")


def test_invalid_origin_is_rejected():
    with pytest.raises(ValueError):
        mp.sign_entry("u1", "user", "x", origin="root", tenant="acme")


def test_v2_sidecar_annotates_authority(tmp_path, monkeypatch):
    _ready_key(monkeypatch)
    path = tmp_path / "AGENT.md"
    entries = ["agent note", "user preference", "trusted tool fact"]
    _write(path, entries)
    mp.sync_entries(
        path, "u1", "agent", entries,
        origins=["agent", "user", "trusted_tool"], tenant="acme",
    )
    meta = mp.annotate_entries(path, "u1", "agent", entries, tenant="acme")
    assert meta[0] == {
        "verified": True, "status": "signed", "origin": "agent",
        "act_class": "inform", "taint": "agent", "key_status": "ready",
    }
    assert meta[1]["origin"] == "user"
    assert meta[1]["act_class"] == "act"
    assert meta[1]["taint"] == "trusted"
    assert meta[2]["origin"] == "trusted_tool"
    assert meta[2]["act_class"] == "act"


def test_legacy_v1_sidecar_is_untrusted_not_elevated(tmp_path):
    path = tmp_path / "USER.md"
    _write(path, ["legacy fact"])
    side = mp.sidecar_path(path)
    side.write_text(
        json.dumps({mp.content_hash("legacy fact"): _legacy_signature("u1", "user", "legacy fact")}),
        encoding="utf-8",
    )
    meta = mp.annotate_entries(path, "u1", "user", ["legacy fact"], tenant="")
    assert meta[0]["verified"] is False
    assert meta[0]["status"] == "legacy-signed"
    assert meta[0]["origin"] == "legacy_untrusted"
    assert meta[0]["act_class"] == "none"
    assert meta[0]["taint"] == "untrusted"

    side.write_text(
        json.dumps({mp.content_hash("legacy fact"): _legacy_signature("u1", "user", "other")}),
        encoding="utf-8",
    )
    tampered = mp.annotate_entries(path, "u1", "user", ["legacy fact"], tenant="")
    assert tampered[0]["status"] == "tampered"
    assert tampered[0]["origin"] == "untrusted_external"


def test_cross_user_and_target_fail():
    sig = mp.sign_entry("u1", "agent", "fact", origin="agent", tenant="")
    assert not mp.verify_entry("u2", "agent", "fact", sig, origin="agent", tenant="")
    assert not mp.verify_entry("u1", "user", "fact", sig, origin="agent", tenant="")


def test_cross_tenant_sidecar_copy_is_tampered(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, tenant="acme", experimental=True)
    src = tmp_path / "acme" / "u1" / "AGENT.md"
    _write(src, ["shared fact"])
    mp.sync_entries(src, "u1", "agent", ["shared fact"], origins=["agent"])

    dst = tmp_path / "beta" / "u1" / "AGENT.md"
    _write(dst, ["shared fact"])
    shutil.copyfile(mp.sidecar_path(src), mp.sidecar_path(dst))

    monkeypatch.setenv("WT_TENANT_ID", "beta")
    meta = mp.annotate_entries(dst, "u1", "agent", ["shared fact"])
    assert meta[0]["verified"] is False
    assert meta[0]["status"] == "tampered"
    assert meta[0]["origin"] == "untrusted_external"


def test_sidecar_write_is_atomic_and_0600(tmp_path, monkeypatch):
    path = tmp_path / "AGENT.md"
    _write(path, ["a"])
    mp.sync_entries(path, "u1", "agent", ["a"], origins=["agent"], tenant="")
    side = mp.sidecar_path(path)
    assert stat.S_IMODE(side.stat().st_mode) == 0o600
    before = side.read_text(encoding="utf-8")

    def _boom(_src, _dst):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(mp.os, "replace", _boom)
    mp.sync_entries(path, "u1", "agent", ["a", "b"], origins=["agent", "agent"], tenant="")
    assert side.read_text(encoding="utf-8") == before
    assert not list(path.parent.glob(".*tmp*"))


def test_sidecar_rollback_leaves_new_entries_unverified(tmp_path):
    path = tmp_path / "AGENT.md"
    _write(path, ["a"])
    mp.sync_entries(path, "u1", "agent", ["a"], origins=["agent"], tenant="")
    old = mp.sidecar_path(path).read_text(encoding="utf-8")

    _write(path, ["a", "b"])
    mp.sync_entries(path, "u1", "agent", ["a", "b"], origins=["agent", "agent"], tenant="")
    mp.sidecar_path(path).write_text(old, encoding="utf-8")

    meta = mp.annotate_entries(path, "u1", "agent", ["a", "b"], tenant="")
    assert meta[0]["status"] == "signed"
    assert meta[1]["status"] == "legacy-unsigned"
    assert meta[1]["verified"] is False


def test_memory_tool_does_not_relaunder_legacy_entries(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    user = SimpleNamespace(id="u-relaunder")
    path = tmp_path / "u-relaunder" / "USER.md"
    _write(path, ["legacy fact"])

    added = json.loads(asyncio.run(memory(
        {"action": "add", "target": "user", "content": "new fact"}, user=user,
    )))
    assert added["count"] == 2

    meta = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"}, user=user,
    )))["entries_meta"]
    assert meta[0]["status"] == "legacy-unsigned"
    assert meta[0]["origin"] == "legacy_untrusted"
    assert meta[0]["verified"] is False
    assert meta[1]["status"] == "signed"
    assert meta[1]["origin"] == "agent"

    asyncio.run(memory({"action": "remove", "target": "user", "key": "new fact"}, user=user))
    after = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"}, user=user,
    )))["entries_meta"]
    assert after[0]["status"] == "legacy-unsigned"
    assert after[0]["origin"] == "legacy_untrusted"


def test_strict_mode_quarantines_unverified(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from app.tools import memory as memory_mod

    user = SimpleNamespace(id="u-strict")
    path = tmp_path / "u-strict" / "AGENT.md"
    _write(path, ["legacy fact"])
    asyncio.run(memory({"action": "add", "target": "agent", "content": "signed fact"}, user=user))

    default_read = json.loads(asyncio.run(memory(
        {"action": "read", "target": "agent"}, user=user,
    )))
    assert len(default_read["entries"]) == 2
    assert "quarantined_entries" not in default_read

    monkeypatch.setitem(memory_mod.config.security, "memory_provenance_strict", True)
    strict_read = json.loads(asyncio.run(memory(
        {"action": "read", "target": "agent"}, user=user,
    )))
    assert len(strict_read["entries"]) == 1
    assert strict_read["entries_meta"][0]["origin"] == "agent"
    assert strict_read["quarantine_count"] == 1
    assert strict_read["quarantined_entries"][0]["origin"] == "legacy_untrusted"


def test_agent_memory_dir_is_tenant_scoped(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, tenant="acme", experimental=True)
    from app.tools.memory import _get_memory_dir, _get_memory_path

    assert _get_memory_dir() == tmp_path / "acme"
    assert _get_memory_path("user", "u1", ensure_dir=False) == tmp_path / "acme" / "u1" / "USER.md"


def test_no_tenant_keeps_legacy_path(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, tenant=None)
    from app.tools.memory import _get_memory_dir

    assert _get_memory_dir() == tmp_path


def test_tenant_without_experimental_is_refused(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, tenant="acme", experimental=None)
    from app.tools.memory import _get_memory_dir

    assert tenant_config_errors()
    with pytest.raises(TenantConfigError):
        _get_memory_dir()


def test_memory_tool_description_states_authority(monkeypatch):
    from app.tools.memory import memory as _memory_tool  # noqa: F401  (registers schema)
    from app.tools.registry import registry

    desc = registry.get_schema("memory")["description"]
    assert "act_class" in desc
    assert "untrusted_external" in desc
    assert "legacy_untrusted" in desc
    assert "terminal" in desc


def test_duplicate_content_with_different_origins_is_not_signed(tmp_path):
    path = tmp_path / "USER.md"
    entries = ["same fact", "same fact"]
    _write(path, entries)
    mp.sync_entries(path, "u1", "user", entries, origins=["agent", None], tenant="")
    meta = mp.annotate_entries(path, "u1", "user", entries, tenant="")
    assert [m["status"] for m in meta] == ["legacy-unsigned", "legacy-unsigned"]
    assert all(m["origin"] == "legacy_untrusted" for m in meta)


def test_memory_tool_returns_structured_error_when_tenant_misconfigured(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, tenant="acme", experimental=None)
    payload = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"},
        user=SimpleNamespace(id="u-misconfigured"),
    )))
    assert payload["error"] == "tenant_mode_misconfigured"
    assert "multi_tenant_experimental" in payload["reason"]


def test_strict_mode_all_quarantined_declares_missing_evidence(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from app.tools import memory as memory_mod

    user = SimpleNamespace(id="u-all-quarantined")
    path = tmp_path / "u-all-quarantined" / "AGENT.md"
    _write(path, ["legacy only"])
    monkeypatch.setitem(memory_mod.config.security, "memory_provenance_strict", True)
    payload = json.loads(asyncio.run(memory(
        {"action": "read", "target": "agent"}, user=user,
    )))
    assert payload["entries"] == []
    assert payload["count"] == 0
    assert payload["evidence_status"] == "quarantined"
    assert payload["missing_evidence"] is True
    assert payload["quarantine_count"] == 1


def test_no_op_replace_preserves_legacy_origin(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    user = SimpleNamespace(id="u-noop")
    path = tmp_path / "u-noop" / "USER.md"
    _write(path, ["poison text"])

    result = json.loads(asyncio.run(memory(
        {"action": "replace", "target": "user", "old": "poison", "content": "poison text"},
        user=user,
    )))
    assert result["count"] == 1
    meta = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"}, user=user,
    )))["entries_meta"]
    assert meta[0]["status"] == "legacy-unsigned"
    assert meta[0]["origin"] == "legacy_untrusted"


def test_remove_then_add_identical_content_is_tombstoned(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    user = SimpleNamespace(id="u-tombstone")
    path = tmp_path / "u-tombstone" / "USER.md"
    _write(path, ["poison text", "keep this"])

    asyncio.run(memory({"action": "remove", "target": "user", "key": "poison"}, user=user))
    asyncio.run(memory({"action": "add", "target": "user", "content": "poison text"}, user=user))

    payload = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"}, user=user,
    )))
    by_entry = {e.split("] ", 1)[1]: m for e, m in zip(payload["entries"], payload["entries_meta"])}
    assert by_entry["poison text"]["status"] == "legacy-unsigned"
    assert by_entry["poison text"]["origin"] == "legacy_untrusted"
    assert by_entry["keep this"]["status"] == "legacy-unsigned"


def test_trailing_whitespace_is_canonicalized(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    user = SimpleNamespace(id="u-space")
    asyncio.run(memory(
        {"action": "add", "target": "user", "content": "fact one\n"},
        user=user,
    ))
    meta = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"}, user=user,
    )))["entries_meta"]
    assert meta[0]["status"] == "signed"
    assert meta[0]["origin"] == "agent"

    asyncio.run(memory(
        {"action": "replace", "target": "user", "old": "fact one",
         "content": "  fact two  "},
        user=user,
    ))
    meta2 = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"}, user=user,
    )))["entries_meta"]
    assert meta2[0]["status"] == "signed"
    assert meta2[0]["origin"] == "agent"


def test_sync_entries_without_origins_does_not_sign(tmp_path):
    path = tmp_path / "AGENT.md"
    _write(path, ["a"])
    mp.sync_entries(path, "u1", "agent", ["a"], tenant="")
    assert not mp.sidecar_path(path).exists()
    meta = mp.annotate_entries(path, "u1", "agent", ["a"], tenant="")
    assert meta[0]["status"] == "legacy-unsigned"


def test_v2_origin_relabel_is_tampered(tmp_path):
    path = tmp_path / "AGENT.md"
    _write(path, ["fact"])
    mp.sync_entries(path, "u1", "agent", ["fact"], origins=["agent"], tenant="")
    side = mp.sidecar_path(path)
    doc = json.loads(side.read_text(encoding="utf-8"))
    key = next(iter(doc["entries"]))
    doc["entries"][key]["origin"] = "user"
    side.write_text(json.dumps(doc), encoding="utf-8")

    meta = mp.annotate_entries(path, "u1", "agent", ["fact"], tenant="")
    assert meta[0]["status"] == "tampered"
    assert meta[0]["origin"] == "untrusted_external"
    assert meta[0]["verified"] is False


def test_malformed_non_ascii_signature_fails_closed(tmp_path):
    path = tmp_path / "AGENT.md"
    _write(path, ["fact"])
    side = mp.sidecar_path(path)
    side.write_text(json.dumps({
        "version": 2,
        "entries": {
            mp.content_hash("fact"): {"sig": "签名", "origin": "agent", "tenant": ""},
        },
    }), encoding="utf-8")
    meta = mp.annotate_entries(path, "u1", "agent", ["fact"], tenant="")
    assert meta[0]["status"] == "tampered"
    assert meta[0]["verified"] is False


def test_non_ascii_tenant_segments_are_stable_and_distinct(monkeypatch):
    from app.services.tenant_service import tenant_config_errors, tenant_segment

    monkeypatch.delenv("WT_MULTI_TENANT_EXPERIMENTAL", raising=False)
    monkeypatch.setenv("WT_TENANT_ID", "公司")
    first = tenant_segment()
    assert first
    assert tenant_config_errors()  # non-empty segment, but opt-in missing
    monkeypatch.setenv("WT_TENANT_ID", "华北研究院")
    second = tenant_segment()
    assert second and second != first
    monkeypatch.setenv("WT_MULTI_TENANT_EXPERIMENTAL", "true")
    assert tenant_config_errors() == []
    monkeypatch.setenv("WT_TENANT_ID", "a" * 70)
    assert len(tenant_segment()) <= 64


def test_identity_loader_filters_unverified_under_strict(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from app.core.config import get_config
    from app.services import agent_service

    uid = "u-identity"
    path = tmp_path / uid / "USER.md"
    _write(path, ["用户的名字是 Bob。"])

    async def fake_judge(_prompt, _payload, **_kwargs):
        return {"identity_entries": ["用户的名字是 Bob。"]}

    monkeypatch.setattr("app.services.agentic_judge.judge_json", fake_judge)
    agent_service._IDENTITY_MEMORY_CACHE.clear()
    loose = asyncio.run(agent_service._load_identity_memory_context(uid))
    assert "Bob" in loose

    monkeypatch.setitem(get_config().security, "memory_provenance_strict", True)
    agent_service._IDENTITY_MEMORY_CACHE.clear()
    strict = asyncio.run(agent_service._load_identity_memory_context(uid))
    assert strict == ""


def test_empty_store_remove_then_add_is_tombstoned(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    user = SimpleNamespace(id="u-empty-tombstone")
    path = tmp_path / "u-empty-tombstone" / "USER.md"
    _write(path, ["poison text"])

    asyncio.run(memory({"action": "remove", "target": "user", "key": "poison"}, user=user))
    assert not path.exists()
    asyncio.run(memory({"action": "add", "target": "user", "content": "poison text"}, user=user))

    meta = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"}, user=user,
    )))["entries_meta"]
    assert meta[0]["status"] == "legacy-unsigned"
    assert meta[0]["origin"] == "legacy_untrusted"


def test_replace_then_readd_old_text_is_tombstoned(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    user = SimpleNamespace(id="u-replace-tombstone")
    path = tmp_path / "u-replace-tombstone" / "USER.md"
    _write(path, ["legacy text", "anchor"])

    asyncio.run(memory({
        "action": "replace", "target": "user", "old": "anchor",
        "content": "new anchor",
    }, user=user))
    asyncio.run(memory({"action": "add", "target": "user", "content": "anchor"}, user=user))

    payload = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"}, user=user,
    )))
    by_entry = {e.split("] ", 1)[1]: m for e, m in zip(payload["entries"], payload["entries_meta"])}
    assert by_entry["anchor"]["status"] == "legacy-unsigned"


def test_remove_then_replace_with_removed_text_is_tombstoned(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    user = SimpleNamespace(id="u-remove-replace")
    path = tmp_path / "u-remove-replace" / "USER.md"
    _write(path, ["untrusted line", "anchor"])

    asyncio.run(memory({"action": "remove", "target": "user", "key": "untrusted"}, user=user))
    asyncio.run(memory({
        "action": "replace", "target": "user", "old": "anchor",
        "content": "untrusted line",
    }, user=user))

    payload = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"}, user=user,
    )))
    by_entry = {e.split("] ", 1)[1]: m for e, m in zip(payload["entries"], payload["entries_meta"])}
    assert by_entry["untrusted line"]["status"] == "legacy-unsigned"


def test_readding_agent_authored_text_keeps_agent_origin(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    user = SimpleNamespace(id="u-agent-tombstone")
    asyncio.run(memory({"action": "add", "target": "user", "content": "agent fact"}, user=user))
    asyncio.run(memory({"action": "add", "target": "user", "content": "keep"}, user=user))
    asyncio.run(memory({"action": "remove", "target": "user", "key": "agent fact"}, user=user))
    asyncio.run(memory({"action": "add", "target": "user", "content": "agent fact"}, user=user))

    payload = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"}, user=user,
    )))
    by_entry = {e.split("] ", 1)[1]: m for e, m in zip(payload["entries"], payload["entries_meta"])}
    assert by_entry["agent fact"]["status"] == "signed"
    assert by_entry["agent fact"]["origin"] == "agent"


def test_tombstone_pruning_keeps_newest(tmp_path, monkeypatch):
    path = tmp_path / "AGENT.md"
    _write(path, [])
    monkeypatch.setattr(mp, "MAX_TOMBSTONES", 2)
    times = iter([1.0, 2.0, 3.0])
    monkeypatch.setattr(mp.time, "time", lambda: next(times))
    for text in ("aaa", "bbb", "ccc"):
        mp.sync_entries(
            path, "u1", "agent", [], origins=[],
            tombstones={mp.content_hash(text): None}, tenant="",
        )
    removed = mp.load_removed(mp.sidecar_path(path))
    assert set(removed) == {mp.content_hash("bbb"), mp.content_hash("ccc")}


def test_partial_remove_rolls_back_when_tombstone_write_fails(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    user = SimpleNamespace(id="u-remove-rollback")
    path = tmp_path / "u-remove-rollback" / "USER.md"
    _write(path, ["poison text", "keep"])

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mp, "_atomic_write_json", boom)
    result = json.loads(asyncio.run(memory(
        {"action": "remove", "target": "user", "key": "poison"}, user=user,
    )))
    assert result["error"] == "tombstone_write_failed"
    text = path.read_text(encoding="utf-8")
    assert "poison text" in text
    assert "keep" in text


def test_read_key_status_matches_entries(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    user = SimpleNamespace(id="u-key-status")
    asyncio.run(memory({"action": "add", "target": "user", "content": "fact"}, user=user))

    first = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"}, user=user,
    )))
    assert first["provenance_key_status"] == first["entries_meta"][0]["key_status"] == "dev"

    _ready_key(monkeypatch)
    second = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"}, user=user,
    )))
    assert second["provenance_key_status"] == second["entries_meta"][0]["key_status"] == "ready"


def test_dev_key_caps_user_act_authority(tmp_path, monkeypatch):
    from app.core.config import get_config

    monkeypatch.setitem(
        get_config().security, "memory_hmac_key", "weave-memory-provenance-dev",
    )
    path = tmp_path / "AGENT.md"
    _write(path, ["user fact"])
    mp.sync_entries(path, "u1", "agent", ["user fact"], origins=["user"], tenant="")
    meta = mp.annotate_entries(path, "u1", "agent", ["user fact"], tenant="")
    assert meta[0]["origin"] == "user"
    assert meta[0]["act_class"] == "inform"
    assert meta[0]["taint"] == "agent"
    assert meta[0]["key_status"] == "dev"


def test_memory_authority_key_ready_rejects_dev_key(monkeypatch):
    from app.core.config import get_config

    cfg = get_config()
    assert cfg.memory_authority_key_ready is False
    monkeypatch.setitem(cfg.security, "memory_hmac_key", "x" * 32)
    assert cfg.memory_authority_key_ready is True
    monkeypatch.setitem(
        cfg.security, "memory_hmac_key",
        "dev-secret-key-change-in-production-weave-thinker-2026",
    )
    assert cfg.memory_authority_key_ready is False
    monkeypatch.setitem(cfg.security, "memory_hmac_key", "short")
    assert cfg.memory_authority_key_ready is False


def test_flags_default_off():
    from app.core.config import get_config

    cfg = get_config()
    assert cfg.memory_provenance_strict is False
    assert cfg.multi_tenant_experimental is False
