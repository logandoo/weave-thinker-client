"""TDD tests — EoT flush hold while local mic speech is present (ASR gap
bridge, 2026-09-20).

FunASR realtime 结果流在长句边界可停滞数秒而用户仍在说话（真实 77.5s 录音
实测 58.3→65.0s 空窗 6.7s，conv 213f5a55 类），EoT watchdog 把这段当静音并
flush 出「未说完就抢答」的 turn。上游不给结果时，浏览器上传的本地麦克风
PCM 能量是唯一可用的活动信号：自适应噪声底 + 近场比值判定，将有本地语音的
flush 延后（有上限 `eot_activity_extension_seconds`），等待 ASR 恢复续传。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_voice_eot_activity_bridge.py -v
"""
import asyncio
import time

import numpy as np

from app.services import voice_service
from app.services.voice_service import VoiceDuplexSession


class _DummyWS:
    def __init__(self):
        self.sent = []
        self.json_events = []

    async def send_bytes(self, data):
        self.sent.append(data)

    async def send_text(self, text):
        self.json_events.append(text)


def _make_session():
    ws = _DummyWS()
    s = VoiceDuplexSession(ws, None, None, conversation_id=None)

    async def _send_json(ev):
        ws.json_events.append(ev)

    s._send_json = _send_json
    return s, ws


def _pcm16(amplitude: int, n: int = 1600) -> bytes:
    return np.full(n, amplitude, dtype=np.int16).tobytes()


def test_track_mic_energy_updates_activity_on_speech():
    s, ws = _make_session()
    assert s._last_mic_activity == 0.0
    s._track_mic_energy(_pcm16(6000))
    assert s._last_mic_activity > 0.0, "loud near-field PCM must count as activity"


def test_track_mic_energy_does_not_activate_on_silence():
    s, ws = _make_session()
    s._track_mic_energy(_pcm16(6000))
    t1 = s._last_mic_activity
    time.sleep(0.01)
    s._track_mic_energy(_pcm16(20))
    assert s._last_mic_activity == t1, "silence must not refresh activity"


def test_hold_flush_bounded_and_gated(monkeypatch):
    s, ws = _make_session()
    monkeypatch.setitem(s.config.voice, "eot_activity_bridge", True)
    monkeypatch.setitem(s.config.voice, "eot_activity_extension_seconds", 0.4)
    monkeypatch.setitem(s.config.voice, "barge_in_proximity_gate", True)

    # Near-field speech just heard → hold.
    s._last_mic_activity = time.monotonic()
    assert s._hold_flush_for_mic_activity() is True

    # Beyond the per-turn extension cap → no hold (bounded), even if a window
    # reset would otherwise start a fresh window.
    s._flush_hold_turn_started = time.monotonic() - 1.0
    s._flush_hold_since = time.monotonic() - 0.1
    assert s._hold_flush_for_mic_activity() is False
    assert s._flush_hold_turn_started == 0.0, "cap release must reset the turn anchor"

    # Stale activity (user stopped) → no hold.
    s._last_mic_activity = time.monotonic() - 5.0
    assert s._hold_flush_for_mic_activity() is False

    # Far-field mic (TV/room) → no hold.
    s._last_mic_activity = time.monotonic()
    s._prox_seen = True
    s._prox_near = False
    s._prox_updated = time.monotonic()
    assert s._hold_flush_for_mic_activity() is False

    # Disabled by config → no hold.
    s._prox_seen = False
    monkeypatch.setitem(s.config.voice, "eot_activity_bridge", False)
    assert s._hold_flush_for_mic_activity() is False


def test_hold_window_resets_on_new_asr_result(monkeypatch):
    """A new ASR result supersedes the pending flush: the hold budget is
    per flush-decision, not per session-turn (A4.9 r2 regression guard)."""
    s, ws = _make_session()
    monkeypatch.setitem(s.config.voice, "eot_activity_bridge", True)
    monkeypatch.setitem(s.config.voice, "eot_activity_extension_seconds", 0.4)
    now = time.monotonic()
    s._last_mic_activity = now
    s._flush_hold_since = now - 0.1
    s._flush_hold_turn_started = now - 10.0
    s._last_text_change = now  # fresh recognizer result supersedes the decision
    assert s._hold_flush_for_mic_activity() is True
    assert s._flush_hold_turn_started > now - 0.5, "budget must restart per decision"


def test_hold_flush_active_in_every_playback_state(monkeypatch):
    """A paused answer / aux playback must not block the bridge — a live user
    monologue would otherwise be fragmented by playback-bookkeeping states."""
    s, ws = _make_session()
    monkeypatch.setitem(s.config.voice, "eot_activity_bridge", True)
    s._last_mic_activity = time.monotonic()
    s._speaking = True
    assert s._hold_flush_for_mic_activity() is True
    s._playback_paused = True
    assert s._hold_flush_for_mic_activity() is True
