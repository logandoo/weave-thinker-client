"""TDD tests — semantic-complete flush must skip the fragment-merge wait
(2026-09-20).

语义 EoT 判定为 complete 的无标点语句（如「好的就这样」）仍需在 responder 端
白等 `fragment_merge_seconds`（默认 1.0s）才起生成 —— 判定结果已经表明文本
完整，没有必要再等合并窗（用户报告的「安静后还要等很久」的一部分）。修复：
flush 时把语义判定随 turn 入队（`semantic_complete`），responder 传给
`_coalesce_fragments(skip_wait=True)`；非语义 complete 的碎片仍按原窗口等待。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_voice_eot_semantic_merge_skip.py -v
"""
import asyncio
import time

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


def test_coalesce_skips_wait_when_semantic_complete(monkeypatch):
    async def run():
        s, ws = _make_session()
        monkeypatch.setitem(s.config.voice, "fragment_merge_seconds", 0.5)
        t0 = time.monotonic()
        merged = await s._coalesce_fragments("好的就这样", skip_wait=True)
        elapsed = time.monotonic() - t0
        assert merged == "好的就这样"
        assert elapsed < 0.2, f"semantic-complete turn still waited {elapsed:.2f}s"

    asyncio.run(run())


def test_coalesce_waits_when_not_semantic_complete(monkeypatch):
    async def run():
        s, ws = _make_session()
        monkeypatch.setitem(s.config.voice, "fragment_merge_seconds", 0.3)
        t0 = time.monotonic()
        await s._coalesce_fragments("我想说", skip_wait=False)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.25, f"merge wait disappeared (elapsed {elapsed:.2f}s)"

    asyncio.run(run())


def test_enqueue_turn_carries_semantic_flag():
    s, ws = _make_session()
    s._enqueue_turn("好的就这样", semantic_complete=True)
    item = s._turn_queue.get_nowait()
    assert item.get("semantic_complete") is True, item
    assert item.get("text") == "好的就这样", item
