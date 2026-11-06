"""P3.2 conditional section selection (2026-10-18)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.prompts.selector import SAFETY_SECTIONS, select_sections


def test_safety_always_present():
    for mode in ("chat", "voice", "deathmatch", "worker", "subagent"):
        got = select_sections(mode)
        for s in SAFETY_SECTIONS:
            assert s in got


def test_research_and_coding_differ():
    research = select_sections("chat", "research")
    coding = select_sections("chat", "coding")
    assert "citation_rules" in research
    assert "coding_rules" in coding
    assert "coding_rules" not in research


def test_available_filter_keeps_safety():
    got = select_sections("chat", "coding", available=["platform_rules"])
    for s in SAFETY_SECTIONS:
        assert s in got
    assert "coding_rules" not in got


def test_filter_sections_never_drops_safety_core():
    from app.prompts.selector import filter_sections
    from app.prompts.spec import PromptSection

    sections = (
        PromptSection("platform_rules", "p"),
        PromptSection("coding_rules", "c"),
        PromptSection("safety_core", "s", tier="safety_core"),
    )
    kept = filter_sections(sections, "voice", "creative")
    ids = {s.id for s in kept}
    assert "safety_core" in ids
    assert "coding_rules" not in ids


def test_select_specs_respects_scope():
    from app.prompts.selector import select_specs
    from app.prompts.spec import PromptSpec

    specs = [
        PromptSpec("voice.duplex", "1.0.0", "voice", scope={"mode": "voice"}),
        PromptSpec("agent.system", "1.0.0", "agent-core", scope={"mode": "chat"}),
        PromptSpec("memory.boundary", "1.0.0", "memory", scope={"mode": "*"}),
    ]
    ids = {s.id for s in select_specs(specs, "voice")}
    assert ids == {"voice.duplex", "memory.boundary"}
