"""S1: execution-state tree v1 (2026-10-18)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.execution_state_service import (
    ABANDONED,
    DONE,
    ExecutionStateTree,
)


def test_grow_compress_render_active_path():
    t = ExecutionStateTree("c1")
    a = t.grow(kind="trace", step_id="s1", summary="read main.py")
    b = t.grow(kind="trace", step_id="s1", summary="run tests")
    s = t.compress([a.id, b.id], step_id="s1", summary="step 1 done")
    assert s is not None
    assert a.status == DONE and b.status == DONE
    text = t.render()
    assert "step 1 done" in text
    assert "execution_state" in text


def test_revise_abandons_failed_branch_and_keeps_hint():
    t = ExecutionStateTree("c1")
    boundary = t.grow(kind="trace", step_id="s2", summary="attempt A")
    marker = t.revise(boundary.id, reason="tests failed", error_class="assert")
    assert marker is not None
    assert boundary.status == ABANDONED
    text = t.render()
    assert "不要重复" in text
    assert "tests failed" in text


def test_maintain_marks_failed_when_missing():
    t = ExecutionStateTree("c1")
    n = t.grow(kind="summary", step_id="s1", summary="done")
    out = t.maintain(n.id, missing=["path X", "test Y"])
    assert out["ok"] is False
    assert n.status == "failed"


def test_maintain_marks_done_without_missing():
    t = ExecutionStateTree("c1")
    n = t.grow(kind="summary", step_id="s1", summary="done")
    out = t.maintain(n.id)
    assert out["ok"] is True
    assert n.status == DONE


def test_render_plan_hints_does_not_duplicate_plan():
    from app.services.execution_state_service import render_plan_hints
    hints = render_plan_hints({"steps": [
        {"id": "s1", "status": "done", "recovery": "idle"},
        {"id": "s2", "status": "failed", "recovery": "retry", "failure_reason": "tests failed"},
    ]})
    assert "s2" in hints
    assert "s1" not in hints
    assert "唯一权威" in hints
