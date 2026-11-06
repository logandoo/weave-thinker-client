"""P0/P1 prompt package contracts (2026-10-18)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.prompts.compiler import PromptCompileError, compile_prompt, lint_text
from app.prompts.registry import manifest, require
from app.prompts.spec import PromptSection, PromptSpec


def test_memory_boundary_spec_compiles_and_hashes():
    spec = require("memory.boundary")
    compiled = compile_prompt(spec)
    assert "不是指令" in compiled.text
    assert "事实或证据" in compiled.text
    assert len(compiled.prompt_hash) == 64
    assert compiled.estimated_tokens > 0


def test_duplicate_rule_id_rejected():
    spec = PromptSpec(
        id="test.dup", version="1.0.0", owner="test",
        sections=(
            PromptSection("a", "x", rule_ids=("R-1",)),
            PromptSection("b", "y", rule_ids=("R-1",)),
        ),
    )
    try:
        compile_prompt(spec)
        assert False, "duplicate rule id accepted"
    except PromptCompileError:
        pass


def test_budget_enforced():
    spec = PromptSpec(
        id="test.budget", version="1.0.0", owner="test",
        sections=(PromptSection("a", "x" * 400),),
    )
    try:
        compile_prompt(spec, budget_tokens=1)
        assert False, "budget not enforced"
    except PromptCompileError:
        pass


def test_lint_adjacent_duplicate_numbering():
    assert lint_text("1. a\n2. b\n7. c\n7. d\n")
    assert not lint_text("1. a\n2. b\n7. c\n\n8. d\n7. e\n")


def test_manifest_contains_memory_boundary():
    ids = {m["id"] for m in manifest()}
    assert "memory.boundary" in ids


def test_delegate_prompt_only_claims_offered_tools():
    from app.tools.delegate_task import _build_child_system_prompt
    only_search = _build_child_system_prompt(
        "g", available_tools=["web_search"],
    )
    assert "web_search" in only_search
    assert "browser (for browsing" not in only_search
    assert "execute_code (for running" not in only_search
    none = _build_child_system_prompt("g", available_tools=[])
    assert "Use only the tools present in your tool schema." in none


def test_dead_grilling_prompt_removed():
    from app.services import deathmatch_service as ds
    assert not hasattr(ds, "GRILLING_SYSTEM_PROMPT")
    assert not hasattr(ds.DeathmatchManager, "get_grilling_system_prompt")
