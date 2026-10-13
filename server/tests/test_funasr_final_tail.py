"""TDD tests for Fun-ASR final-text tail inclusion (会议记录截尾 fix).

The Fun-ASR streaming proxy accumulates finalized (sentence_end=True) segments,
but on task-finished it previously emitted ONLY those segments — dropping the
last in-progress sentence that had been displayed as partial text.
The caller passes ``pending_tail`` = the latest in-progress sentence (empty
when the last result was finalized), so verbatim-repetition tails are kept too.

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_funasr_final_tail.py -v
"""
import pytest

from app.services.asr_service import _compose_funasr_final_text


class TestComposeFunasrFinalText:
    def test_empty_tail_returns_finalized(self):
        assert _compose_funasr_final_text(["你好。", "世界。"], "") == "你好。世界。"

    def test_pending_tail_is_appended(self):
        # Last sentence still in progress (no sentence_end) must not be lost.
        assert _compose_funasr_final_text(["你好。"], "世界") == "你好。世界"

    def test_empty_everything(self):
        assert _compose_funasr_final_text([], "") == ""

    def test_no_finalized_only_tail(self):
        assert _compose_funasr_final_text([], "只有半句") == "只有半句"

    def test_verbatim_repetition_tail_kept(self):
        # "好的。好的。" — the pending tail equals the finalized suffix and
        # must still be appended (the old endswith check dropped it).
        assert _compose_funasr_final_text(["好的。"], "好的。") == "好的。好的。"

    def test_tail_after_long_meeting(self):
        assert _compose_funasr_final_text(["靶向治疗。", "定期评估。"], "调理身体") == "靶向治疗。定期评估。调理身体"
