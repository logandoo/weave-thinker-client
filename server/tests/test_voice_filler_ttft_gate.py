"""TDD tests for the TTFT-gated voice filler (填充词首字门控) — 2026-09-13.

用户需求：填充词（"嗯嗯"/"等下啊"/"我想想"）此前几乎每轮都播，听感很差。
新机制：发送 query 后等待 `voice_filler_ttft_threshold_seconds`（默认 0.7s）——
若模型在该窗口内已产出首个 content token，则本轮不播填充词；若未产出，才播。
判据为 AND：elapsed >= threshold AND 首字未到 AND 未在播报/暂停/打断 AND 冷却窗通过。
（OR 语义会退化为旧行为：时间总会长过阈值，等于每轮都播——故必须 AND。）

三段保留：
  arm      — EoT watchdog 静默窗口内投机预取合成（不变）。
  convert  — flush 时不再立即入队；暂存为 `_pending_filler`（不发事件/不盖冷却）。
  watch    — 主 LLM 调用开始时 schedule；睡满阈值后按 AND 判据决定播/弃。
             barge-in 路径无预取时，慢轮由 `_speak_filler` 兜底合成播出。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_voice_filler_ttft_gate.py -v
"""
import asyncio
from contextlib import contextmanager

from app.core.config import get_config
from app.services.voice_service import VoiceDuplexSession, _now


class _DummyTTS:
    async def stream_tts(self, text, style_instruction=None, timeout=60.0):
        for _ in range(6):
            await asyncio.sleep(0.001)
            yield b"\x00" * 960


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


def _is_thinking_off(body) -> bool:
    if not isinstance(body, dict):
        return False
    return (
        body.get("enable_thinking") is False
        or (body.get("thinking") or {}).get("type") == "disabled"
    )


def _filler_events(ws):
    return [e for e in ws.json_events if e.get("event") == "filler"]


def _aux_items(s):
    return [
        it for it in list(s._tts_queue._queue)
        if isinstance(it, dict) and it.get("text") and it.get("aux")
    ]


@contextmanager
def _threshold(value):
    cfg = get_config()
    voice = cfg._config.setdefault("voice", {})
    saved = voice.get("filler_ttft_threshold_seconds")
    voice["filler_ttft_threshold_seconds"] = value
    try:
        yield
    finally:
        if saved is None:
            voice.pop("filler_ttft_threshold_seconds", None)
        else:
            voice["filler_ttft_threshold_seconds"] = saved


async def _cleanup(s):
    tasks = []
    if s._filler_watch_task is not None:
        tasks.append(s._filler_watch_task)
    tasks.extend(s._tasks)
    for t in tasks:
        if not t.done():
            t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


UTTERANCE = "今天天气怎么样"


# ---------- 1. convert: 不再立即入队，暂存 pending ----------

def test_convert_holds_pending_without_playback():
    async def run():
        s, ws = _make_session()
        s._arm_filler_prefetch(UTTERANCE)
        q = s._filler_prefetch["q"]
        phrase = s._filler_prefetch["phrase"]
        await s._convert_filler_prefetch(UTTERANCE)
        assert _aux_items(s) == [], "convert 不得立即入队（首字门控前）"
        assert _filler_events(ws) == [], "convert 不得立即发 filler 事件"
        assert s._last_filler_at == 0.0, "convert 不得提前盖冷却戳"
        assert s._pending_filler["phrase"] == phrase
        assert s._pending_filler["q"] is q, "pending 必须携带预合成队列"
        assert s._filler_prefetch["q"] is None, "预取一次性清空"
        await _cleanup(s)
    asyncio.run(run())


def test_convert_still_gated_by_noise_and_stopword():
    async def run():
        s, ws = _make_session()
        s._arm_filler_prefetch(UTTERANCE)
        await s._convert_filler_prefetch("嗯啊咳")
        assert s._pending_filler["phrase"] == ""
        assert _filler_events(ws) == []
        s2, ws2 = _make_session()
        s2._arm_filler_prefetch(UTTERANCE)
        await s2._convert_filler_prefetch("停下")
        assert s2._pending_filler["phrase"] == ""
        await _cleanup(s)
        await _cleanup(s2)
    asyncio.run(run())


# ---------- 2. watch: 快轮抑制 / 慢轮播出 ----------

def test_fast_content_suppresses_filler():
    async def run():
        with _threshold(0.2):
            s, ws = _make_session()
            s._turn_epoch = 1
            s._arm_filler_prefetch(UTTERANCE)
            await s._convert_filler_prefetch(UTTERANCE)
            s._schedule_filler_ttft_watch()
            # 模型在阈值内产出首字
            s._answer_content_epoch = s._turn_epoch
            await asyncio.sleep(0.3)
            assert _filler_events(ws) == [], "首字已到 → 不得播填充词"
            assert _aux_items(s) == []
            await _cleanup(s)
    asyncio.run(run())


def test_slow_content_plays_prefetched_filler():
    async def run():
        with _threshold(0.05):
            s, ws = _make_session()
            s._turn_epoch = 1
            s._arm_filler_prefetch(UTTERANCE)
            q = s._filler_prefetch["q"]
            phrase = s._filler_prefetch["phrase"]
            await s._convert_filler_prefetch(UTTERANCE)
            s._schedule_filler_ttft_watch()
            await asyncio.sleep(0.2)
            evs = _filler_events(ws)
            assert len(evs) == 1, "慢轮必须恰好播一次填充词"
            assert evs[0]["text"] == phrase
            items = _aux_items(s)
            assert len(items) == 1
            assert items[0]["pre_q"] is q, "慢轮必须复用预合成音频"
            assert items[0]["epoch"] == s._turn_epoch
            assert s._last_filler_at > 0, "真正播出才盖冷却戳"
            assert any(g[0] == phrase for g in s._aux_echo_guard), "播出必须登记回声守卫"
            await _cleanup(s)
    asyncio.run(run())


def test_watch_respects_epoch_after_turn_changed():
    async def run():
        with _threshold(0.05):
            s, ws = _make_session()
            s._turn_epoch = 1
            s._arm_filler_prefetch(UTTERANCE)
            await s._convert_filler_prefetch(UTTERANCE)
            s._schedule_filler_ttft_watch()
            s._turn_epoch = 2  # 新一轮已开始
            await asyncio.sleep(0.15)
            assert _filler_events(ws) == [], "过期轮次不得播填充词"
            await _cleanup(s)
    asyncio.run(run())


# ---------- 3. watch 抑制条件 ----------

def _pending_session():
    s, ws = _make_session()
    s._turn_epoch = 1
    s._arm_filler_prefetch(UTTERANCE)
    return s, ws


def test_watch_suppressed_when_speaking():
    async def run():
        with _threshold(0.05):
            s, ws = _pending_session()
            await s._convert_filler_prefetch(UTTERANCE)
            s._schedule_filler_ttft_watch()
            s._speaking = True
            await asyncio.sleep(0.15)
            assert _filler_events(ws) == []
            await _cleanup(s)
    asyncio.run(run())


def test_watch_suppressed_when_paused():
    async def run():
        with _threshold(0.05):
            s, ws = _pending_session()
            await s._convert_filler_prefetch(UTTERANCE)
            s._schedule_filler_ttft_watch()
            s._playback_paused = True
            await asyncio.sleep(0.15)
            assert _filler_events(ws) == []
            await _cleanup(s)
    asyncio.run(run())


def test_watch_suppressed_when_interrupted():
    async def run():
        with _threshold(0.05):
            s, ws = _pending_session()
            await s._convert_filler_prefetch(UTTERANCE)
            s._schedule_filler_ttft_watch()
            s._interrupt.set()
            await asyncio.sleep(0.15)
            assert _filler_events(ws) == []
            await _cleanup(s)
    asyncio.run(run())


def test_watch_suppressed_within_cooldown():
    async def run():
        with _threshold(0.05):
            s, ws = _pending_session()
            await s._convert_filler_prefetch(UTTERANCE)
            s._schedule_filler_ttft_watch()
            s._last_filler_at = _now()
            await asyncio.sleep(0.15)
            assert _filler_events(ws) == []
            await _cleanup(s)
    asyncio.run(run())


# ---------- 4. barge-in 兜底 + 清理 ----------

def test_barge_in_path_falls_back_to_speak_filler():
    async def run():
        with _threshold(0.05):
            s, ws = _make_session()
            s._turn_epoch = 1
            s._schedule_filler_ttft_watch()
            await asyncio.sleep(0.2)
            assert len(_filler_events(ws)) == 1, "无预取的慢轮必须兜底合成播出"
            assert _aux_items(s), "兜底路径必须入队 aux 项"
            await _cleanup(s)
    asyncio.run(run())


def test_clear_pending_cancels_watch():
    async def run():
        with _threshold(5.0):
            s, ws = _pending_session()
            await s._convert_filler_prefetch(UTTERANCE)
            s._schedule_filler_ttft_watch()
            watch = s._filler_watch_task
            assert watch is not None and not watch.done()
            s._clear_pending_filler()
            await asyncio.sleep(0.01)
            assert s._filler_watch_task is None
            assert watch.cancelled() or watch.done()
            assert s._pending_filler["phrase"] == ""
            assert _filler_events(ws) == []
            await _cleanup(s)
    asyncio.run(run())


def test_drain_clears_pending_and_watch():
    async def run():
        with _threshold(5.0):
            s, ws = _pending_session()
            await s._convert_filler_prefetch(UTTERANCE)
            s._schedule_filler_ttft_watch()
            s._drain_tts_queue()
            await asyncio.sleep(0.01)
            assert s._pending_filler["phrase"] == ""
            assert s._filler_watch_task is None
            await _cleanup(s)
    asyncio.run(run())


# ---------- 5. 配置 ----------

def test_config_ttft_threshold_default_and_clamp():
    cfg = get_config()
    assert cfg.voice_filler_ttft_threshold_seconds == 0.7
    with _threshold(-3):
        assert cfg.voice_filler_ttft_threshold_seconds == 0.0
    with _threshold(1.5):
        assert cfg.voice_filler_ttft_threshold_seconds == 1.5


# ---------- 6. 生成路径接线：首字纪元 + 快路径清 pending ----------

class _FakeStreamLLM:
    def __init__(self, delay: float):
        self.delay = delay
        self.kwargs = None

    async def stream_chat_structured(self, messages, **kwargs):
        self.kwargs = kwargs
        await asyncio.sleep(self.delay)
        yield {"type": "content", "data": "好的呀。"}


def test_generation_fast_stream_marks_content_epoch_no_filler():
    async def run():
        with _threshold(0.25):
            s, ws = _make_session()
            s._turn_epoch = 0
            s._arm_filler_prefetch(UTTERANCE)
            await s._convert_filler_prefetch(UTTERANCE)
            fake = _FakeStreamLLM(delay=0.0)
            s._build_llm = lambda purpose="voice": (fake, "m")

            async def fake_persist(role, content, **kw):
                pass

            async def fake_speak(text, wait=True, aux=False, **kwargs):
                pass

            s._persist_message = fake_persist
            s._speak_text = fake_speak
            consumer = asyncio.create_task(s._tts_consumer())
            try:
                await s._generate_and_speak_impl(UTTERANCE)
            finally:
                s._closed = True
                s._interrupt.set()
                try:
                    s._tts_queue.put_nowait(None)
                except Exception:
                    pass
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            assert s._answer_content_epoch == s._turn_epoch, "首字必须标记本轮纪元"
            assert _filler_events(ws) == [], "快流不得播填充词"
            assert _is_thinking_off(fake.kwargs.get("extra_body")), (
                f"主生成路径必须关思考: {fake.kwargs.get('extra_body')}"
            )
            await _cleanup(s)
    asyncio.run(run())


def test_prefetched_fast_path_clears_pending_and_marks_epoch():
    async def run():
        with _threshold(0.05):
            s, ws = _make_session()
            s._pending_filler = {"phrase": "嗯，我想想", "q": asyncio.Queue()}
            s._arm_filler_prefetch(UTTERANCE)
            await s._convert_filler_prefetch(UTTERANCE)

            async def fake_persist(role, content, **kw):
                pass

            async def fake_speak(text, wait=True, aux=False, **kwargs):
                pass

            s._persist_message = fake_persist
            s._speak_text = fake_speak
            await s._generate_and_speak_impl(UTTERANCE, prefetched="你好呀。")
            assert s._pending_filler["phrase"] == "", "预取快路径必须清 pending"
            assert s._answer_content_epoch == s._turn_epoch
            await asyncio.sleep(0.1)
            assert _filler_events(ws) == []
            await _cleanup(s)
    asyncio.run(run())


# ---------- 7. A4.9 修复回归：生成异常结束取消 watch / allow_filler=False / synth 取消 ----------

class _FailingStreamLLM:
    async def stream_chat_structured(self, messages, **kwargs):
        raise RuntimeError("provider down")
        yield  # pragma: no cover


class _SlowTTS:
    async def stream_tts(self, text, style_instruction=None, timeout=60.0):
        for _ in range(50):
            await asyncio.sleep(0.02)
            yield b"\x00" * 960


def test_generation_error_cancels_watch_no_filler():
    """A4.9 Important #1：生成异常/空回复结束不得留下 watch 在静默中播填充词。"""
    async def run():
        with _threshold(0.05):
            s, ws = _make_session()
            s._arm_filler_prefetch(UTTERANCE)
            await s._convert_filler_prefetch(UTTERANCE)
            s._build_llm = lambda purpose="voice": (_FailingStreamLLM(), "m")

            async def fake_persist(role, content, **kw):
                pass

            s._persist_message = fake_persist
            await s._generate_and_speak(UTTERANCE)
            await asyncio.sleep(0.15)
            assert _filler_events(ws) == [], "生成异常结束后不得播填充词"
            assert s._pending_filler["phrase"] == ""
            await _cleanup(s)
    asyncio.run(run())


def test_allow_filler_false_clears_pending():
    """bg-task notice 路径（allow_filler=False）不得播填充词。"""
    async def run():
        with _threshold(0.05):
            s, ws = _make_session()
            s._arm_filler_prefetch(UTTERANCE)
            await s._convert_filler_prefetch(UTTERANCE)
            assert s._pending_filler["phrase"]
            s._build_llm = lambda purpose="voice": (_FailingStreamLLM(), "m")

            async def fake_persist(role, content, **kw):
                pass

            s._persist_message = fake_persist
            await s._generate_and_speak(UTTERANCE, allow_filler=False)
            assert s._pending_filler["phrase"] == "", "notice 路径必须清 pending"
            await asyncio.sleep(0.1)
            assert _filler_events(ws) == []
            await _cleanup(s)
    asyncio.run(run())


def test_clear_pending_cancels_running_synth_task():
    """A4.9 Minor #2：被丢弃的预合成任务必须取消，不再填充无人读的队列。"""
    async def run():
        s, ws = _make_session()
        s.tts = _SlowTTS()
        s._arm_filler_prefetch(UTTERANCE)
        await s._convert_filler_prefetch(UTTERANCE)
        task = s._pending_filler.get("task")
        assert task is not None and not task.done()
        s._clear_pending_filler()
        await asyncio.sleep(0.01)
        assert task.cancelled(), "丢弃 pending 必须取消预合成任务"
        await _cleanup(s)
    asyncio.run(run())
