"""TDD tests for voice filler prefix (填充词) + backchannel (应和).

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_voice_aux_speech.py -v
"""
import asyncio
import time

import pytest

from app.core.config import get_config
from app.services.voice_service import (
    VoiceDuplexSession,
    _pick_aux_phrase,
    _strip_aux_echo,
)


class _DummyTTS:
    def __init__(self, counts=None):
        self.counts = counts or {}
        self.produced = {}

    async def stream_tts(self, text, style_instruction=None, timeout=60.0):
        n = self.counts.get(text, 10)
        self.produced[text] = 0
        for _ in range(n):
            self.produced[text] += 1
            await asyncio.sleep(0.001)
            yield b"\x00" * 960  # 0.02s @ 24kHz mono PCM16


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
    s.tts = _DummyTTS()

    async def _send_bytes(b):
        ws.sent.append(b)

    async def _send_json(ev):
        ws.json_events.append(ev)

    s._send_bytes = _send_bytes
    s._send_json = _send_json
    return s, ws


# ---------- 1. phrase picker ----------

def test_pick_aux_phrase_random_no_immediate_repeat():
    phrases = ["我来看看啊", "我来想想啊", "等下啊，我琢磨一下", "嗯，我想想"]
    seen = set()
    last = ""
    for _ in range(200):
        p = _pick_aux_phrase(phrases, last)
        assert p in phrases
        assert p != last
        seen.add(p)
        last = p
    assert len(seen) >= 3  # randomness exercised


def test_pick_aux_phrase_single_and_empty():
    assert _pick_aux_phrase(["嗯"], "嗯") == "嗯"
    assert _pick_aux_phrase([], "") == ""
    assert _pick_aux_phrase(["", None, "  "], "") == ""


def test_pick_aux_phrase_recent_exclusion():
    """P5（2026-09-05）：排除近期已选（非仅上一条），根治"几乎总是同一句"。"""
    phrases = ["嗯", "嗯嗯", "哦", "对", "嗯哼", "对啊"]
    # recent 成员在池有剩余时永不命中（last 也在 recent 语义内）
    for _ in range(200):
        p = _pick_aux_phrase(phrases, "嗯", recent=["嗯嗯", "哦"])
        assert p not in ("嗯", "嗯嗯", "哦")
        assert p in phrases
    # recent 覆盖到只剩 last 之外一条 → 确定命中它
    p = _pick_aux_phrase(phrases, "嗯", recent=["嗯嗯", "哦", "对", "嗯哼"])
    assert p == "对啊"
    # recent 全覆盖 → 兜底 last-only 排除
    p = _pick_aux_phrase(phrases, "嗯", recent=["嗯嗯", "哦", "对", "嗯哼", "对啊"])
    assert p in phrases and p != "嗯"
    # last+recent 双全覆盖 → 全池兜底不空
    p = _pick_aux_phrase(phrases, "", recent=list(phrases))
    assert p in phrases
    # 旧签名兼容（recent 缺省 = 仅排除 last）
    assert _pick_aux_phrase(["嗯"], "嗯") == "嗯"


def test_pick_aux_phrase_no_three_consecutive_with_recent_tracking():
    """池 >2 时同短语不得连续 3 次选中（会话侧 recent 追踪的接线语义仿真）。"""
    phrases = ["我来看看啊", "我来想想啊", "等下啊，我琢磨一下", "嗯，我想想"]
    last = ""
    recent = []
    picks = []
    for _ in range(300):
        p = _pick_aux_phrase(phrases, last, recent=recent)
        picks.append(p)
        last = p
        recent = (recent + [p])[-3:]
    for i in range(len(picks) - 2):
        assert not (picks[i] == picks[i + 1] == picks[i + 2]), (
            f"连续 3 次选中同一短语 {picks[i]!r}（位置 {i}）"
        )


# ---------- 7. aux-echo strip ----------

def test_strip_aux_echo_only_suffix_of_longer_text():
    now = 1000.0
    g = lambda ph, age=0.5: (ph, now - age)
    assert _strip_aux_echo("帮我查一下天气嗯", [g("嗯")], now=now) == "帮我查一下天气"
    assert _strip_aux_echo("好的等下啊，我琢磨一下",
                           [g("等下啊，"), g("我琢磨一下"), g("等下啊，我琢磨一下")], now=now) == "好的"
    assert _strip_aux_echo("嗯", [g("嗯")], now=now) == "嗯"          # standalone ack never stripped
    assert _strip_aux_echo("好的嗯嗯", [g("嗯")], now=now) == "好的嗯"  # one instance stripped
    assert _strip_aux_echo("abc", [g("嗯")], now=now) == "abc"
    assert _strip_aux_echo("", [g("嗯")], now=now) == ""
    # stale guards never rewrite user speech (A4.9 M2)
    assert _strip_aux_echo("我自己琢磨一下", [g("我琢磨一下", age=6.0)], now=now) == "我自己琢磨一下"
    # plain-string guards still accepted (backward compat)
    assert _strip_aux_echo("帮我查一下嗯", ["嗯"], now=now) == "帮我查一下"


# ---------- 8. config ----------

def test_voice_config_new_keys():
    cfg = get_config()
    assert cfg.voice_filler_enabled is True
    assert "我来看看啊" in cfg.voice_filler_phrases
    assert len(cfg.voice_filler_phrases) >= 4
    assert cfg.voice_backchannel_enabled is True
    assert cfg.voice_backchannel_phrases
    assert 0 < cfg.voice_backchannel_pause_seconds < cfg.voice_eot_semantic_probe_seconds
    assert cfg.voice_backchannel_cooldown_seconds > 0
    assert cfg.voice_backchannel_max_per_turn >= 1


# ---------- 2. filler trigger gates ----------

def test_speak_filler_gates():
    asyncio.run(_test_speak_filler_gates())


async def _test_speak_filler_gates():
    s, ws = _make_session()
    cfg = get_config()
    # happy path
    await s._speak_filler()
    assert any(ev.get("event") == "filler" for ev in ws.json_events)
    items = list(s._tts_queue._queue)
    assert items and all(it.get("aux") for it in items)
    # Segment-agnostic: phrases like "嗯，我想想" are split at the comma into
    # multiple aux segments, so compare the joined text, not per-item equality
    # (the old per-item check was a 25% random flake — pre-existing, surfaced
    # 2026-08-25).
    joined = "".join(it.get("text", "") for it in items)
    assert any(p in joined for p in cfg.voice_filler_phrases)

    # gate: speaking
    s2, ws2 = _make_session()
    s2._speaking = True
    await s2._speak_filler()
    assert not ws2.json_events
    assert s2._tts_queue.empty()

    # gate: playback paused
    s3, ws3 = _make_session()
    s3._playback_paused = True
    await s3._speak_filler()
    assert not ws3.json_events
    assert s3._tts_queue.empty()

    # gate: tools running
    s4, ws4 = _make_session()
    s4._tools_running = True
    await s4._speak_filler()
    assert not ws4.json_events
    assert s4._tts_queue.empty()

    # gate: closed
    s5, ws5 = _make_session()
    s5._closed = True
    await s5._speak_filler()
    assert not ws5.json_events


def test_speak_filler_no_immediate_repeat():
    asyncio.run(_test_speak_filler_no_immediate_repeat())


async def _test_speak_filler_no_immediate_repeat():
    s, ws = _make_session()
    await s._speak_filler()
    first = [it.get("text", "") for it in s._tts_queue._queue]
    s._tts_queue = asyncio.Queue()
    ws.json_events.clear()
    await s._speak_filler()
    second = [it.get("text", "") for it in s._tts_queue._queue]
    assert "".join(first) != "".join(second) or len(first) == 1  # single-seg phrase may repeat only if same


# ---------- 3/5/6/10. consumer: aux cut + serialization + pools ----------

async def _run_consumer_until_idle(s, pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.005)
    return False


def test_consumer_aux_cut_by_answer_and_pool_isolation():
    asyncio.run(_test_consumer_aux_cut_by_answer_and_pool_isolation())


async def _test_consumer_aux_cut_by_answer_and_pool_isolation():
    s, ws = _make_session()
    s.tts = _DummyTTS(counts={"嗯": 80, "这是一个答案来了。": 30})
    consumer = asyncio.create_task(s._tts_consumer())
    try:
        await s._tts_queue.put({"text": "嗯", "epoch": 1, "aux": True, "dedup": False})
        ok = await _run_consumer_until_idle(s, lambda: bool(ws.sent))
        assert ok, "aux should start playing"
        await s._tts_queue.put({"text": "这是一个答案来了。", "epoch": 1, "dedup": True})
        ok = await _run_consumer_until_idle(
            s,
            lambda: (
                s._turn_segments
                and s._turn_segments[-1].get("text") == "这是一个答案来了。"
                and not s._consumer_playing
            ),
        )
        assert ok, "answer should complete after cutting the aux filler"
        # aux truncated at PLAYBACK (synthesis runs ahead of playback, so the
        # produced count is not the play count): the number of chunks actually
        # SENT must be far below aux(80) + answer(30) = 110.
        assert len(ws.sent) < 110, f"aux was not cut: {len(ws.sent)} chunks sent"
        assert s.tts.produced.get("这是一个答案来了。", 0) == 30
        # I1 regression: the FIRST answer chunk must be played, not dropped
        # (the aux-cut probe must put the chunk back before breaking)
        assert len(ws.sent) >= 30, "answer audio must be fully played"
        assert s.tts.produced.get("嗯", 0) >= 1
        # pool isolation: aux never enters _turn_segments
        assert len(s._turn_segments) == 1
        assert s._turn_segments[0]["text"] == "这是一个答案来了。"
        # dup window holds only the answer
        assert "嗯" not in s._dup_window._segs
    finally:
        s._closed = True
        s._interrupt.set()
        try:
            s._tts_queue.put_nowait(None)
        except Exception:
            pass
        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass
        for t in s._tasks:
            t.cancel()
        await asyncio.gather(*s._tasks, return_exceptions=True)


def test_consumer_aux_plays_when_no_answer_comes():
    asyncio.run(_test_consumer_aux_plays_when_no_answer_comes())


async def _test_consumer_aux_plays_when_no_answer_comes():
    s, ws = _make_session()
    s.tts = _DummyTTS(counts={"嗯": 5})
    consumer = asyncio.create_task(s._tts_consumer())
    try:
        await s._tts_queue.put({"text": "嗯", "epoch": 1, "aux": True, "dedup": False})
        ok = await _run_consumer_until_idle(
            s,
            lambda: s.tts.produced.get("嗯", 0) == 5 and not s._consumer_playing,
        )
        assert ok, "aux should play to completion when nothing follows"
        assert s.tts.produced.get("嗯", 0) == 5
        assert s._turn_segments == [], "aux must never enter the breakpoint pool"
    finally:
        s._closed = True
        s._interrupt.set()
        try:
            s._tts_queue.put_nowait(None)
        except Exception:
            pass
        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass
        for t in s._tasks:
            t.cancel()
        await asyncio.gather(*s._tasks, return_exceptions=True)


def test_consumer_plain_answer_path_unchanged():
    asyncio.run(_test_consumer_plain_answer_path_unchanged())


async def _test_consumer_plain_answer_path_unchanged():
    s, ws = _make_session()
    s.tts = _DummyTTS(counts={"这是一个普通回答。": 20})
    consumer = asyncio.create_task(s._tts_consumer())
    try:
        await s._tts_queue.put({"text": "这是一个普通回答。", "epoch": 1, "dedup": True})
        ok = await _run_consumer_until_idle(
            s,
            lambda: s.tts.produced.get("这是一个普通回答。", 0) == 20 and not s._consumer_playing,
        )
        assert ok, "plain answer should play fully"
        assert s.tts.produced.get("这是一个普通回答。", 0) == 20
        assert len(s._turn_segments) == 1
        assert s._turn_segments[0]["text"] == "这是一个普通回答。"
        assert "这是一个普通回答" in s._dup_window._segs
    finally:
        s._closed = True
        s._interrupt.set()
        try:
            s._tts_queue.put_nowait(None)
        except Exception:
            pass
        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass
        for t in s._tasks:
            t.cancel()
        await asyncio.gather(*s._tasks, return_exceptions=True)


# ---------- 4. backchannel trigger gates ----------

def _bc_session():
    s, ws = _make_session()
    s._asr_ready = True
    s._speaking = False
    s._interjecting = False
    s._turn_active = False
    s._playback_paused = False
    s._last_backchannel_time = 0.0
    s._backchannel_acked_text = ""
    s._backchannel_count_this_turn = 0
    s._prox_seen = True
    s._prox_near = True
    s._prox_updated = time.time()
    return s, ws


def test_maybe_backchannel_happy_path():
    asyncio.run(_test_maybe_backchannel_happy_path())


async def _test_maybe_backchannel_happy_path():
    s, ws = _bc_session()
    await s._maybe_backchannel(0.4, "我想查一下今天的天")
    items = list(s._tts_queue._queue)
    assert items and all(it.get("aux") for it in items)
    assert any(ev.get("event") == "agent_backchannel" for ev in ws.json_events)
    assert s._backchannel_count_this_turn == 1
    assert s._backchannel_acked_text == "我想查一下今天的天"


def test_maybe_backchannel_gates():
    asyncio.run(_test_maybe_backchannel_gates())


async def _test_maybe_backchannel_gates():
    cfg = get_config()

    async def fires(**overrides):
        s, ws = _bc_session()
        for k, v in overrides.items():
            setattr(s, k, v)
        await s._maybe_backchannel(0.4, "我想查一下今天的天")
        return bool(list(s._tts_queue._queue))

    assert not await fires(_asr_ready=False)
    assert not await fires(_speaking=True)
    assert not await fires(_interjecting=True)
    assert not await fires(_turn_active=True)
    assert not await fires(_playback_paused=True)
    assert not await fires(_prox_near=False)
    assert not await fires(_closed=True)

    # silence window
    s, ws = _bc_session()
    await s._maybe_backchannel(cfg.voice_backchannel_pause_seconds - 0.1, "我想查一下今天的天")
    assert s._tts_queue.empty()
    s, ws = _bc_session()
    await s._maybe_backchannel(cfg.voice_eot_semantic_probe_seconds, "我想查一下今天的天")
    assert s._tts_queue.empty()

    # complete utterance / noise / short
    s, ws = _bc_session()
    await s._maybe_backchannel(0.4, "我想查一下今天的天气。")
    assert s._tts_queue.empty()
    s, ws = _bc_session()
    await s._maybe_backchannel(0.4, "嗯")
    assert s._tts_queue.empty()
    s, ws = _bc_session()
    await s._maybe_backchannel(0.4, "对")
    assert s._tts_queue.empty()

    # echo of own recent speech
    s, ws = _bc_session()
    s._spoken_text_recent = "嗯，我来想想啊"
    await s._maybe_backchannel(0.4, "我来想想啊")
    assert s._tts_queue.empty()

    # cooldown
    s, ws = _bc_session()
    s._last_backchannel_time = time.time()
    await s._maybe_backchannel(0.4, "我想查一下今天的天")
    assert s._tts_queue.empty()

    # same-text dedup
    s, ws = _bc_session()
    s._backchannel_acked_text = "我想查一下今天的天"
    await s._maybe_backchannel(0.4, "我想查一下今天的天")
    assert s._tts_queue.empty()

    # per-turn cap
    s, ws = _bc_session()
    s._backchannel_count_this_turn = cfg.voice_backchannel_max_per_turn
    await s._maybe_backchannel(0.4, "我想查一下今天的天")
    assert s._tts_queue.empty()


def test_maybe_backchannel_no_repeat_phrase():
    asyncio.run(_test_maybe_backchannel_no_repeat_phrase())


async def _test_maybe_backchannel_no_repeat_phrase():
    s, ws = _bc_session()
    await s._maybe_backchannel(0.4, "我想查一下今天的天")
    first = "".join(it.get("text", "") for it in s._tts_queue._queue)
    # new text, same session: next phrase must differ from the last one
    s._tts_queue = asyncio.Queue()
    ws.json_events.clear()
    s._backchannel_acked_text = ""
    s._backchannel_count_this_turn = 0
    s._last_backchannel_time = 0.0
    await s._maybe_backchannel(0.4, "那帮我看看明天的天气")
    second = "".join(it.get("text", "") for it in s._tts_queue._queue)
    assert first != second
