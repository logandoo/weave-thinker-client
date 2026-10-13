"""TDD: playback_drained 必须推进 burst 断点锚（60 生产事故 conv 19b63be9）。

事故链：答案全文（270 字 / 63.52s 音频）播完并 drain → 插话 aux 项作为
客户端新 burst 播放 1.72s 时用户 onset 暂停 → _pause_playback 的断点估计
base_chars(0) + 1.72×rate ≈ 7 → defer 恢复重播 263 字旧答案，新回答音频
排队至死。修复：playback_drained 时把 _burst_base_chars/_burst_base_sec
推进到"已听"位置（一切已发送=已听）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_voice_drain_anchor.py -v
"""
import asyncio

from app.services.voice_service import VoiceDuplexSession


FULL_ANSWER = (
    "（理解）懂了，你爸就是典型的老人心态——嘴上说别买别买，其实是心疼钱，不想让你破费。"
    "真买回来了，他嘴上嫌弃，心里肯定是高兴的。那既然这样，你就别问他了，问也问不出真话。"
    "你就按自己的想法来，买辆合适的大车，直接当礼物送过去，说是给家里添的、全家都能开，"
    "这样他心理负担也小些——不是专门给他一个人买的，是给家里用的。不过正因为他不注重养护、"
    "又心疼钱，选车的时候反而要多替他考虑：好开、好停、省油、维修保养方便，别选太娇贵太费心"
    "的车，不然他开起来反而有负担。你心里有没有大致看中的车型？"
)
assert len(FULL_ANSWER) == 240  # 测试文本长度锚定，防编辑漂移


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

    async def _noop_mark():
        s._speaking = False

    s._mark_speaking_end = _noop_mark
    s._spawned = []

    def _fake_spawn(text, epoch, q, ignore_paused=False):
        s._spawned.append(text)

        async def _idle():
            q.put_nowait(None)

        return asyncio.create_task(_idle())

    s._spawn_synth = _fake_spawn
    return s, ws


def _fill_played_answer(s, text: str, audio_total: float, n_seg: int = 3):
    """Record the whole answer as fully-sent segments (consumer 口径)."""
    seg_len = len(text) // n_seg
    cum = 0.0
    for i in range(n_seg):
        seg_text = text[i * seg_len:] if i == n_seg - 1 else text[i * seg_len:(i + 1) * seg_len]
        sec = audio_total / n_seg
        cum += sec
        s._turn_segments.append({
            "text": seg_text, "audio_sec": sec, "cum_sec": cum, "epoch": s._turn_epoch,
        })
    s._turn_audio_sec_total = audio_total
    s._turn_reply_text = text


# ---------- 1. 事故复现：drain 后 aux burst 期间暂停 → 不得重播答案 ----------

def test_pause_after_drain_during_aux_replays_nothing():
    async def run():
        s, ws = _make_session()
        _fill_played_answer(s, FULL_ANSWER, 63.52)

        # 答案播完：客户端 drain（一切已发送=已听）
        await s._handle_client_event({"event": "playback_drained"})

        # 插话 aux 新 burst 播放 1.72s（客户端 played_sec 自该 burst 重计）
        s._playing_aux = True
        s._current_seg_text = "60多岁确实要考虑上下车方便"
        s._current_seg_audio_sec = 1.9
        s._playback_played_sec = 1.72

        await s._pause_playback()

        assert s._paused_spoken_chars >= len(FULL_ANSWER), (
            f"断点 {s._paused_spoken_chars} 落后于答案末尾 {len(FULL_ANSWER)}"
            "——恢复会把已播完的旧答案重播给用户"
        )
        assert s._spawned == [], f"不应预合成任何 remainder: {[t[:20] for t in s._spawned]}"

        # defer 裁决 → resume：必须无残留可播
        s._playback_paused = True  # _pause_playback 已置位；保持语义
        await s._resume_playback()
        assert s._tts_queue.empty(), "resume 不得向 TTS 队列写入任何重播内容"
        assert any(ev.get("event") == "playback_resumed" for ev in ws.json_events)
    asyncio.run(run())


# ---------- 2. 回归：答案中段暂停（无 drain）断点仍在中间 ----------

def test_pause_mid_answer_without_drain_unchanged():
    async def run():
        s, _ = _make_session()
        # 前 114 字 / 31.76s 已播完
        half = FULL_ANSWER[:114]
        s._turn_segments.append({"text": half, "audio_sec": 31.76, "cum_sec": 31.76, "epoch": s._turn_epoch})
        s._turn_audio_sec_total = 31.76
        s._turn_reply_text = FULL_ANSWER
        s._playback_played_sec = 20.0  # 首 burst 中段

        await s._pause_playback()

        # rate=114/31.76≈3.59 → est≈71（snap 到附近边界）
        assert 50 <= s._paused_spoken_chars <= 100, f"中段断点失真: {s._paused_spoken_chars}"
        assert s._spawned, "中段暂停必须预合成剩余文本"
    asyncio.run(run())


# ---------- 3. drain 发生在答案中段：后续暂停从 drain 锚点继续 ----------

def test_drain_mid_answer_advances_base_then_pause():
    async def run():
        s, _ = _make_session()
        part = FULL_ANSWER[:100]
        s._turn_segments.append({"text": part, "audio_sec": 30.0, "cum_sec": 30.0, "epoch": s._turn_epoch})
        s._turn_audio_sec_total = 30.0
        s._turn_reply_text = FULL_ANSWER

        await s._handle_client_event({"event": "playback_drained"})
        assert s._burst_base_chars == 100
        assert s._burst_base_sec == 30.0

        # 答案继续：第 2 段 60 字 / 15s 录毕；新 burst 已播 10s
        s._playing_aux = False
        s._current_seg_text = ""
        s._current_seg_audio_sec = 0.0
        s._turn_segments.append({"text": FULL_ANSWER[100:160], "audio_sec": 15.0, "cum_sec": 45.0, "epoch": s._turn_epoch})
        s._turn_audio_sec_total = 45.0
        s._playback_played_sec = 10.0

        await s._pause_playback()

        # rate=160/45≈3.56 → est = 100 + 35.6 ≈ 135（无锚时为 35 → 重播 100 字）
        assert s._paused_spoken_chars >= 125, (
            f"断点 {s._paused_spoken_chars} 未从 drain 锚点(100)起算——会重播已听内容"
        )
    asyncio.run(run())


# ---------- 4. 空段 drain（filler 先行）安全无异常 ----------

def test_drain_with_no_segments_is_safe():
    async def run():
        s, _ = _make_session()
        s._playing_aux = True
        s._current_seg_text = "我来看看啊"
        s._current_seg_audio_sec = 1.2
        s._turn_audio_sec_total = 1.2
        await s._handle_client_event({"event": "playback_drained"})
        assert s._burst_base_chars == 0
    asyncio.run(run())


# ---------- 5. A4.9 R1 M2：drain 须清掉上一 burst 的 played_sec 残值 ----------

def test_drain_resets_stale_played_sec():
    async def run():
        s, _ = _make_session()
        _fill_played_answer(s, FULL_ANSWER, 63.52)
        s._playback_played_sec = 40.0  # 上一 burst 最后的进度上报残值
        await s._handle_client_event({"event": "playback_drained"})
        assert s._playback_played_sec == 0.0, (
            "drain 后 played_sec 残值未清——新 burst 首个 progress 事件前（≤250ms）"
            "的暂停会把过期进度叠进断点（overshoot 跳过未听内容）"
        )
    asyncio.run(run())


# ---------- 6. A4.9 R1 M3：插话录段污染不得越过答案末尾 ----------

def test_drain_anchor_clamped_to_answer_text():
    async def run():
        s, _ = _make_session()
        _fill_played_answer(s, FULL_ANSWER, 63.52)
        # 插话被 _speak_text 无 aux 录段（14 字非答案文本）
        s._turn_segments.append({
            "text": "60多岁确实要考虑上下车方便", "audio_sec": 3.3,
            "cum_sec": 66.82, "epoch": s._turn_epoch,
        })
        s._turn_audio_sec_total = 66.82
        await s._handle_client_event({"event": "playback_drained"})
        assert s._burst_base_chars == len(FULL_ANSWER), (
            f"断点基线 {s._burst_base_chars} 越过答案末尾 {len(FULL_ANSWER)}（插话污染）"
        )
    asyncio.run(run())
