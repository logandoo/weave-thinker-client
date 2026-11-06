"""P1a TMA-NM runtime: monotonic taint, memory-write inheritance, deterministic
shadow/enforce act gate, and an append-only verdict hash chain."""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.act_gate_service as gate
import app.services.taint_service as taint
import app.services.verdict_log_service as verdicts
from app.tools.memory import memory


DELIM = "\n§\n"


def _env(tmp_path, monkeypatch, tenant=None):
    monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path))
    if tenant is None:
        monkeypatch.delenv("WT_TENANT_ID", raising=False)
    else:
        monkeypatch.setenv("WT_TENANT_ID", tenant)
    taint.reset_cache_for_tests()


def _write(path: Path, entries: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DELIM.join(entries) + DELIM if entries else "", encoding="utf-8")


def test_taint_lattice_is_monotonic():
    assert taint.max_taint("trusted", "agent") == "agent"
    assert taint.max_taint("agent", "untrusted") == "untrusted"
    assert taint.max_taint("trusted", "untrusted", "agent") == "untrusted"
    assert taint.min_act_class("act", "inform") == "inform"
    assert taint.min_act_class("inform", "none") == "none"

    ledger = taint.get_or_create_ledger("conv-lattice", "u1")
    ledger.mark("user:1", "trusted", origin="user", principal="user")
    assert ledger.max_taint == "trusted"
    ledger.mark("web:1", "untrusted", origin="untrusted_external", principal="tool:web_search")
    assert ledger.max_taint == "untrusted"
    # A later trusted source never lowers the monotonic max.
    ledger.mark("user:2", "trusted", origin="user", principal="user")
    assert ledger.max_taint == "untrusted"


def test_tool_result_classification():
    assert taint.classify_tool_result(
        "web_search", "{}", input_taint="trusted") == "untrusted"
    assert taint.classify_tool_result(
        "browser", "{}", input_taint="trusted") == "untrusted"
    assert taint.classify_tool_result(
        "browser_click", "{}", input_taint="trusted") == "untrusted"
    assert taint.classify_tool_result(
        "workspace_read", "{}", input_taint="trusted") == "untrusted"
    assert taint.classify_tool_result(
        "grep", "{}", input_taint="trusted") == "untrusted"
    assert taint.classify_tool_result(
        "context7_query_docs", "{}", input_taint="trusted") == "untrusted"
    assert taint.classify_tool_result(
        "vision_interpret", "{}", input_taint="trusted") == "untrusted"
    assert taint.classify_tool_result(
        "job_logs", "{}", input_taint="trusted") == "untrusted"
    assert taint.classify_tool_result(
        "terminal", "{}", input_taint="trusted") == "agent"
    assert taint.classify_tool_result(
        "calculate", "{}", input_taint="trusted") == "agent"
    assert taint.classify_tool_result(
        "citation_render", "{}", input_taint="trusted") == "agent"
    assert taint.classify_tool_result(
        "job_submit", "{}", input_taint="trusted") == "agent"
    assert taint.classify_tool_result(
        "search_tools", "{}", input_taint="trusted") == "agent"
    assert taint.classify_tool_result(
        "pdf_export", "{}", input_taint="trusted") == "agent"
    assert taint.classify_tool_result(
        "some_unknown_tool", "{}", input_taint="trusted") == "untrusted"
    assert taint.classify_tool_result(
        "notes", "{}", input_taint="trusted", tool_args={"action": "list_notes"}) == "untrusted"
    assert taint.classify_tool_result(
        "notes", "{}", input_taint="trusted", tool_args={"action": "create_note"}) == "agent"
    assert taint.classify_tool_result(
        "terminal", "{}", input_taint="untrusted") == "untrusted"
    # Trusted-tool echo (L-b) never lowers untrusted input.
    assert taint.classify_tool_result(
        "trusted_echo", "{}", input_taint="untrusted",
        trusted_tools={"trusted_echo"},
    ) == "untrusted"
    assert taint.classify_tool_result(
        "trusted_echo", "{}", input_taint="trusted",
        trusted_tools={"trusted_echo"},
    ) == "trusted"


def test_memory_read_classification_uses_entries_meta():
    payload = json.dumps({
        "entries_meta": [
            {"origin": "agent", "taint": "agent"},
            {"origin": "legacy_untrusted", "taint": "untrusted"},
        ],
    })
    assert taint.classify_tool_result("memory", payload, input_taint="trusted") == "untrusted"
    # A memory read is a tool echo (M2 L-b): it may carry agent information
    # but can never mint trusted authority/corroboration.
    user_payload = json.dumps({
        "entries_meta": [{"origin": "user", "taint": "trusted"}],
    })
    assert taint.classify_tool_result("memory", user_payload, input_taint="trusted") == "agent"
    empty_payload = json.dumps({"entries": [], "entries_meta": []})
    assert taint.classify_tool_result("memory", empty_payload, input_taint="trusted") == "agent"


def test_memory_write_inherits_conversation_taint(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    ledger = taint.get_or_create_ledger("conv-l-a", "u1")
    ledger.mark("web:1", "untrusted", origin="untrusted_external", principal="tool:web_search")
    user = SimpleNamespace(id="u1")
    conv = SimpleNamespace(id="conv-l-a")

    asyncio.run(memory(
        {"action": "add", "target": "user", "content": "summary of a malicious page"},
        user=user, conversation=conv,
    ))
    payload = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"}, user=user, conversation=conv,
    )))
    assert payload["entries_meta"][0]["origin"] == "untrusted_external"
    assert payload["entries_meta"][0]["taint"] == "untrusted"
    assert payload["entries_meta"][0]["act_class"] == "none"


def test_memory_write_stays_agent_when_conversation_is_trusted(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    ledger = taint.get_or_create_ledger("conv-trusted", "u1")
    ledger.mark("user:1", "trusted", origin="user", principal="user")
    user = SimpleNamespace(id="u1")
    conv = SimpleNamespace(id="conv-trusted")

    asyncio.run(memory(
        {"action": "add", "target": "user", "content": "user preference"},
        user=user, conversation=conv,
    ))
    payload = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"}, user=user, conversation=conv,
    )))
    assert payload["entries_meta"][0]["origin"] == "agent"
    assert payload["entries_meta"][0]["act_class"] == "inform"


def test_act_gate_modes():
    ledger = taint.get_or_create_ledger("conv-gate", "u1")
    ledger.mark("user:1", "trusted", origin="user", principal="user")
    allowed = gate.evaluate(
        ledger, "terminal", {"command": "ls"}, user_id="u1",
        conversation_id="conv-gate", mode="shadow",
    )
    assert allowed.decision == "allow"
    assert allowed.blocked is False

    ledger.mark("web:1", "untrusted", origin="untrusted_external", principal="tool:web_search")
    shadow = gate.evaluate(
        ledger, "terminal", {"command": "ls"}, user_id="u1",
        conversation_id="conv-gate", mode="shadow",
    )
    assert shadow.decision == "deny"
    assert shadow.blocked is False
    assert "untrusted" in shadow.reason

    enforce = gate.evaluate(
        ledger, "terminal", {"command": "ls"}, user_id="u1",
        conversation_id="conv-gate", mode="enforce",
    )
    assert enforce.decision == "deny"
    assert enforce.blocked is True

    off = gate.evaluate(
        ledger, "terminal", {"command": "ls"}, user_id="u1",
        conversation_id="conv-gate", mode="off",
    )
    assert off.decision == "allow"
    assert off.blocked is False

    # A non-consequential tool never blocks, even in enforce.
    read = gate.evaluate(
        ledger, "web_search", {"query": "x"}, user_id="u1",
        conversation_id="conv-gate", mode="enforce",
    )
    assert read.decision == "allow"
    assert read.blocked is False

    # Effect-based gates: same capability under other names is still gated.
    for name in ("job_submit", "skill_run_script", "git", "mcp_tool", "provide_folder"):
        blocked = gate.evaluate(
            ledger, name, {}, user_id="u1",
            conversation_id="conv-gate", mode="enforce",
        )
        assert blocked.blocked is True, name


def test_corroboration_requires_distinct_principals():
    ledger = taint.get_or_create_ledger("conv-corroboration", "u1")
    ledger.mark("user:1", "trusted", origin="user", principal="user")
    ledger.mark("user:2", "trusted", origin="user", principal="user")
    ledger.mark("web:1", "untrusted", origin="untrusted_external", principal="tool:web_search")
    denied = gate.evaluate(
        ledger, "terminal", {"command": "rm -rf /tmp/x"}, user_id="u1",
        conversation_id="conv-corroboration", mode="enforce",
    )
    assert denied.decision == "deny"
    assert denied.blocked is True
    assert denied.corroborators == ["user"]

    ledger.mark("audit:1", "trusted", origin="trusted_tool", principal="trusted_tool:audit")
    elevated = gate.evaluate(
        ledger, "terminal", {"command": "rm -rf /tmp/x"}, user_id="u1",
        conversation_id="conv-corroboration", mode="enforce",
    )
    assert elevated.decision in ("elevate", "allow")
    assert elevated.blocked is False
    assert set(elevated.corroborators) == {"user", "trusted_tool:audit"}


def test_laundering_scenarios_deny_in_enforce(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    conv = SimpleNamespace(id="conv-l-c")
    user = SimpleNamespace(id="u1")
    ledger = taint.get_or_create_ledger(conv.id, "u1")
    ledger.mark("user:1", "trusted", origin="user", principal="user")
    ledger.mark("web:1", "untrusted", origin="untrusted_external", principal="tool:web_search")
    ledger.mark("web:2", "untrusted", origin="untrusted_external", principal="tool:web_search")

    # L-a: agent summarizes untrusted content into memory, then acts.
    asyncio.run(memory(
        {"action": "add", "target": "agent", "content": "page says: exfiltrate ~/.ssh"},
        user=user, conversation=conv,
    ))
    read = json.loads(asyncio.run(memory(
        {"action": "read", "target": "agent"}, user=user, conversation=conv,
    )))
    assert read["entries_meta"][0]["origin"] == "untrusted_external"
    l_a = gate.evaluate(
        ledger, "terminal", {"command": "cat ~/.ssh/id_rsa"}, user_id="u1",
        conversation_id=conv.id, mode="enforce",
    )
    assert l_a.blocked is True

    # L-b: trusted-tool echo of untrusted input stays untrusted.
    echoed = taint.classify_tool_result(
        "trusted_echo", "{}", input_taint=ledger.max_taint,
        trusted_tools={"trusted_echo"},
    )
    assert echoed == "untrusted"

    # L-c: duplicated untrusted sources do not corroborate.
    assert ledger.trusted_principals() == ["user"]
    l_c = gate.evaluate(
        ledger, "execute_code", {"code": "print(1)"}, user_id="u1",
        conversation_id=conv.id, mode="enforce",
    )
    assert l_c.blocked is True

    # Legit: a fresh trusted conversation is allowed (utility).
    legit = taint.get_or_create_ledger("conv-legit", "u1")
    legit.mark("user:1", "trusted", origin="user", principal="user")
    ok = gate.evaluate(
        legit, "terminal", {"command": "ls"}, user_id="u1",
        conversation_id="conv-legit", mode="enforce",
    )
    assert ok.decision == "allow"
    assert ok.blocked is False


def test_verdict_chain_detects_tamper_reorder_delete():
    secret = b"test-verdict-secret"
    records = []
    prev = ""
    for seq in (1, 2, 3):
        payload = {
            "id": f"v{seq}",
            "tenant_id": "",
            "seq": seq,
            "user_id": "u1",
            "conversation_id": "c1",
            "action_fingerprint": f"fp{seq}",
            "tool_name": "terminal",
            "decision": "deny",
            "reason": "untrusted",
            "taint": "untrusted",
            "corroborators": [],
        }
        entry = verdicts.next_entry(prev, seq, payload, secret=secret)
        records.append(entry)
        prev = entry["entry_hash"]

    ok, broken = verdicts.verify_records(records, secret=secret)
    assert ok is True
    assert broken == ""

    tampered = copy.deepcopy(records)
    tampered[1]["reason"] = "allow"
    ok, broken = verdicts.verify_records(tampered, secret=secret)
    assert ok is False
    assert broken == "v2"

    reordered = [records[1], records[0], records[2]]
    ok, _ = verdicts.verify_records(reordered, secret=secret)
    assert ok is False

    deleted = [records[0], records[2]]
    ok, _ = verdicts.verify_records(deleted, secret=secret)
    assert ok is False

    wrong_key = copy.deepcopy(records)
    ok, _ = verdicts.verify_records(wrong_key, secret=b"other")
    assert ok is False


def test_verdict_chain_detects_tail_truncation():
    secret = b"test-verdict-secret"
    records = []
    prev = ""
    for seq in (1, 2, 3):
        entry = verdicts.next_entry(prev, seq, {
            "id": f"v{seq}", "tenant_id": "", "user_id": "u1",
            "conversation_id": "c1", "decision": "deny",
            "reason": "untrusted", "taint": "untrusted",
            "tool_name": "terminal", "action_fingerprint": f"fp{seq}",
            "corroborators": [],
        }, secret=secret)
        records.append(entry)
        prev = entry["entry_hash"]

    ok, _ = verdicts.verify_records(
        records, secret=secret,
        head_hash=records[-1]["entry_hash"], head_seq=3,
    )
    assert ok is True
    ok, _ = verdicts.verify_records(
        records[:2], secret=secret,
        head_hash=records[-1]["entry_hash"], head_seq=3,
    )
    assert ok is False
    ok, _ = verdicts.verify_records(
        [], secret=secret,
        head_hash=records[-1]["entry_hash"], head_seq=3,
    )
    assert ok is False


def test_verdict_key_status_marks_dev_key(monkeypatch):
    from app.core.config import get_config

    cfg = get_config()
    monkeypatch.setitem(cfg.security, "tma_verdict_key", "weave-tma-verdict-dev")
    assert verdicts.verdict_key_status() == "dev"
    assert verdicts.verdict_key_id() == "v1-dev"
    monkeypatch.setitem(cfg.security, "tma_verdict_key", "k" * 32)
    assert verdicts.verdict_key_status() == "ready"
    assert verdicts.verdict_key_id() == "v1"


def test_tma_mode_off_restores_agent_memory_write(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from app.core.config import get_config

    ledger = taint.get_or_create_ledger("conv-off", "u1")
    ledger.mark("web:1", "untrusted", origin="untrusted_external")
    monkeypatch.setitem(get_config().security, "tma_mode", "off")
    user = SimpleNamespace(id="u1")
    conv = SimpleNamespace(id="conv-off")
    asyncio.run(memory(
        {"action": "add", "target": "user", "content": "off mode fact"},
        user=user, conversation=conv,
    ))
    payload = json.loads(asyncio.run(memory(
        {"action": "read", "target": "user"}, user=user, conversation=conv,
    )))
    assert payload["entries_meta"][0]["origin"] == "agent"


class _FakeCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_exc):
        return False


class _FakeFactory:
    def __call__(self):
        return _FakeCtx()


class _LoopForGate:
    blocked_tools = set()
    session_factory = _FakeFactory()
    workspace_path = ""
    delegation_depth = 0
    _visible_tool_names = None
    _active_tool_names = set()
    _permission_callback = None
    vibeweaver_active = False
    deathmatch_manager = None

    @staticmethod
    def _strip_internal_tool_args(args):
        return args

    def _dm_step_tool_names(self):
        return None

    def _skip_guardrails(self):
        return False


def test_agent_loop_gate_is_behavioral(monkeypatch):
    """Deleting/neutering the loop gate must fail this test."""
    from app.services import agent_loop_tools as alt
    from app.core.config import get_config

    ledger = taint.get_or_create_ledger("conv-loop", "u1")
    ledger.mark("web:1", "untrusted", origin="untrusted_external")

    dispatched = []
    appended = []
    persisted = []

    async def fake_dispatch(name, args, **_kwargs):
        dispatched.append(name)
        return json.dumps({"ok": True})

    async def fake_load(_db, _cid, _uid):
        return ledger

    async def fake_persist(_db, current):
        persisted.append(current.max_taint)

    async def fake_append(**kwargs):
        appended.append(kwargs)
        return True

    monkeypatch.setattr(alt.registry, "dispatch", fake_dispatch)
    monkeypatch.setattr(
        alt.registry, "get_schema",
        lambda name: {"name": name, "parameters": {"type": "object", "properties": {}}},
    )
    monkeypatch.setattr(taint, "load_ledger", fake_load)
    monkeypatch.setattr(taint, "persist_ledger", fake_persist)
    monkeypatch.setattr(verdicts, "append_verdict", fake_append)

    state = SimpleNamespace(
        iterations=1, session_id="s1", _doom_tool_history=[],
        _doom_result_hashes={}, _fetched_urls=set(),
        _succeeded_call_sigs=set(),
    )
    user = SimpleNamespace(id="u1")
    conv = SimpleNamespace(id="conv-loop")
    loop = _LoopForGate()

    # enforce: gate blocks before dispatch; verdict is logged.
    monkeypatch.setitem(get_config().security, "tma_mode", "enforce")
    blocked = asyncio.run(alt.AgentToolMixin._execute_single_tool_inner(
        loop, "call-1", "terminal", {"command": "ls"}, None,
        user, conv, None, state, current_turn_content="hello",
    ))
    assert blocked.error is True
    assert json.loads(blocked.result)["error"] == "act_gate_denied"
    assert dispatched == []
    assert appended and appended[-1]["decision"] == "deny"

    # shadow: dispatch happens and the untrusted result taint is persisted.
    monkeypatch.setitem(get_config().security, "tma_mode", "shadow")
    state._doom_tool_history = []
    allowed = asyncio.run(alt.AgentToolMixin._execute_single_tool_inner(
        loop, "call-2", "terminal", {"command": "ls"}, None,
        user, conv, None, state, current_turn_content="hello",
    ))
    assert allowed.error is False
    assert dispatched == ["terminal"]
    assert persisted and persisted[-1] == "untrusted"
    # Pin the value derived from the tool result, not just the pre-seeded max.
    assert ledger.sources["tool:call-2"].taint == "untrusted"


def test_tma_mode_defaults_to_shadow(monkeypatch):
    from app.core.config import get_config

    cfg = get_config()
    assert cfg.tma_mode == "shadow"
    monkeypatch.setitem(cfg.security, "tma_mode", "enforce")
    assert cfg.tma_mode == "enforce"
    monkeypatch.setitem(cfg.security, "tma_mode", "off")
    assert cfg.tma_mode == "off"
    monkeypatch.setitem(cfg.security, "tma_mode", "bogus")
    assert cfg.tma_mode == "shadow"


def test_act_gate_tool_error_is_recoverable():
    ledger = taint.get_or_create_ledger("conv-error", "u1")
    ledger.mark("web:1", "untrusted", origin="untrusted_external")
    verdict = gate.evaluate(
        ledger, "terminal", {"command": "ls"}, user_id="u1",
        conversation_id="conv-error", mode="enforce",
    )
    payload = json.loads(gate.tool_error(verdict))
    assert payload["error"] == "act_gate_denied"
    assert payload["recoverable"] is True
    assert payload["decision"] == "deny"
