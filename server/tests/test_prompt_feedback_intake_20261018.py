"""C-10: runtime intake queue → reviewed regression corpus."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import prompt_feedback_intake as intake_mod


def _record(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setenv("WT_PROMPT_FEEDBACK_QUEUE", str(tmp_path / "queue.jsonl"))
    return intake_mod.record_feedback_event(**kwargs)


def test_queue_is_deduped_by_content(tmp_path, monkeypatch):
    kwargs = dict(prompt_id="audit.response", source="audit_reject",
                  input_text="draft", expected="reject", note="missing citation")
    first = _record(tmp_path, monkeypatch, **kwargs)
    second = _record(tmp_path, monkeypatch, **kwargs)
    assert first and first == second
    assert len(intake_mod.read_queue()) == 1


def test_different_reviews_are_kept(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, prompt_id="audit.response",
            source="audit_reject", input_text="draft", expected="reject")
    _record(tmp_path, monkeypatch, prompt_id="audit.response",
            source="audit_reject", input_text="draft", expected="needs_evidence")
    assert len(intake_mod.read_queue()) == 2


def test_intake_merges_and_is_idempotent(tmp_path, monkeypatch):
    queue = tmp_path / "queue.jsonl"
    monkeypatch.setenv("WT_PROMPT_FEEDBACK_QUEUE", str(queue))
    intake_mod.record_feedback_event(
        prompt_id="audit.response", source="audit_reject",
        input_text="bad draft", expected="reject", note="no citation")
    corpus = tmp_path / "regressions.json"
    corpus.write_text("[]\n", encoding="utf-8")

    import scripts.prompt_feedback as feedback
    monkeypatch.setattr(feedback, "CASES", corpus)
    args = SimpleNamespace(queue=str(queue), since_days=0,
                           dry_run=False, apply=True)

    assert feedback.intake(args) == 0
    rows = json.loads(corpus.read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert rows[0]["source"] == "audit_reject"
    assert rows[0]["input"] == "bad draft"

    assert feedback.intake(args) == 0
    assert len(json.loads(corpus.read_text(encoding="utf-8"))) == 1


def test_audit_reject_and_tool_error_hooks_are_wired():
    backend = Path(__file__).resolve().parents[1]
    audit = (backend / "app" / "services" / "agent_loop_audit.py").read_text(
        encoding="utf-8")
    loop = (backend / "app" / "services" / "agent_loop.py").read_text(
        encoding="utf-8")
    assert 'source="audit_reject"' in audit
    assert "record_feedback_event" in audit
    assert "await asyncio.to_thread(" in audit
    assert "await _record_tool_error_intake(results)" in loop
    assert 'source="tool_error"' in loop


def test_default_queue_lives_in_gitignored_output_dir():
    path = intake_mod.queue_path()
    assert path.parent.name == "output_files"


def test_intake_rows_are_pending_not_executable(tmp_path, monkeypatch):
    import scripts.prompt_eval as prompt_eval

    corpus = tmp_path / "regressions.json"
    corpus.write_text(json.dumps([{
        "key": "k", "kind": "intake", "id": "reg-x",
        "prompt_id": "audit.response", "source": "audit_reject",
        "input": "draft", "expected": "reject",
    }]), encoding="utf-8")
    monkeypatch.setattr(prompt_eval, "REGRESSIONS", corpus)
    failures, total = prompt_eval.run()
    assert failures == []
    assert total == 72, "intake rows must not inflate the executable count"
    assert prompt_eval.pending_intake_count() == 1


def test_queue_is_redacted_and_owner_only(tmp_path, monkeypatch):
    queue = tmp_path / "queue.jsonl"
    monkeypatch.setenv("WT_PROMPT_FEEDBACK_QUEUE", str(queue))
    fake_key = "sk-" + "x" * 24
    fake_bearer = "Bearer " + "y" * 16
    intake_mod.record_feedback_event(
        prompt_id="audit.response", source="audit_reject",
        input_text=f"key={fake_key}",
        expected="reject", note=fake_bearer)
    raw = queue.read_text(encoding="utf-8")
    assert fake_key not in raw
    assert fake_bearer not in raw
    assert oct(queue.stat().st_mode & 0o777) == "0o600"

