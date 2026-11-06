"""Move-only mixin invariants (P5.4/P5.5, 2026-10-18)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_grilling_state_members_keep_property_semantics():
    from app.services.deathmatch_service import DeathmatchManager

    for name in ("is_grilling", "is_goal_active", "grilling_progress",
                 "all_grilling_complete"):
        assert isinstance(getattr(DeathmatchManager, name), property), name
    assert not isinstance(DeathmatchManager.activate_grilling, property)


def test_voice_classifier_mixin_methods_are_inherited():
    from app.services.voice_service import VoiceDuplexSession

    for name in ("_classify_barge_in", "_classify_intent", "_classify_eot",
                 "_classify_interjection", "_classify_memory_interjection"):
        assert callable(getattr(VoiceDuplexSession, name)), name


def test_voice_clarification_task_set_is_shared():
    from app.services import voice_generation, voice_service

    assert isinstance(voice_generation._voice_clarification_tasks, set)
    assert voice_service._voice_clarification_tasks is voice_generation._voice_clarification_tasks


def test_voice_response_formats_are_reexported():
    from app.services import voice_service

    for name in ("_INTENT_RESPONSE_FORMAT", "_EOT_RESPONSE_FORMAT",
                 "_BARGE_IN_RESPONSE_FORMAT", "_INTERJECTION_RESPONSE_FORMAT",
                 "_MEMORY_INTERJECTION_RESPONSE_FORMAT"):
        assert getattr(voice_service, name)["type"] == "json_schema"
