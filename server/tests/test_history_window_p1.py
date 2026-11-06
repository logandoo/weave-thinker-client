"""P1 S3: latest-N + token budget never takes the oldest window."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.history_window_service import select_history_window


def _msgs(n, size=100):
    return [{"role": "user", "content": f"{i}:" + "字" * size} for i in range(n)]


def test_latest_n_is_preserved():
    msgs = _msgs(60)
    out = select_history_window(msgs, 40, 0)
    assert out[0]["content"].startswith("20:")
    assert out[-1]["content"].startswith("59:")


def test_token_budget_keeps_newest_and_can_drop_more_than_n():
    msgs = _msgs(20, 1000)
    out = select_history_window(msgs, 40, 2000)
    assert out
    assert out[-1] is msgs[-1]
    assert len(out) < 20


def test_newest_message_is_kept_even_over_budget():
    out = select_history_window(_msgs(3, 10000), 40, 1)
    assert out == [_msgs(3, 10000)[-1]]
