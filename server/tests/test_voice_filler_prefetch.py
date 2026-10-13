"""TDD tests for voice filler prefetch (填充词预取) — 2026-08-25.

用户要求：等待词（"我想想啊"/"等下啊"）必须在结束语音识别（EoT flush）后立刻
播放，以此换取生成回答和 TTS 的时间。此前 filler 在 `_handle_user_turn` 内、
位于用户消息 DB 持久化之后才入队，且其自身 TTS 合成那时才启动——填充词几乎
买不到任何生成时间。

新机制（三段，2026-09-13 TTFT 门控后）：
  arm     — EoT watchdog 静音 ≥ 0.5×min(semantic_probe, complete_grace)（下限
            0.2s）时，空闲门控通过即投机预取合成填充词短语（arm-once：存活
            预取期间文本生长不 re-arm，防长话语 per-partial 泄漏计费合成）。
  convert — 空闲 flush 路径在 `_enqueue_turn` 之前把预取转换为 `_pending_filler`
            （不发事件/不入队/不盖冷却）；填词与内容无关，不要求 arm/flush 文本
            相等；对 FLUSHED 文本复检噪声/停止词/空闲门。
  watch   — 主生成开始时 `_schedule_filler_ttft_watch`；睡满
            `filler_ttft_threshold_seconds` 后，若模型仍未产出首字才播
            （见 test_voice_filler_ttft_gate.py）。快轮零填充词。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_voice_filler_prefetch.py -v
"""
import asyncio

from app.core.config import get_config
from app.services.voice_service import (
    VoiceDuplexSession,
    _is_voice_noise,
    _now,
)


class _DummyTTS:
    async def stream_tts(self, text, style_instruction=None, timeout=60.0):
        for _ in range(10):
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


def _filler_events(ws):
    return [e for e in ws.json_events if e.get("event") == "filler"]


def _tts_items(s):
    return [it for it in list(s._tts_queue._queue) if isinstance(it, dict) and it.get("text")]


UTTERANCE = "今天天气怎么样"


# ---------- arm: EoT 预判点的投机合成 ----------

def test_arm_starts_prefetch_synth():
    async def run():
        s, _ = _make_session()
        s._arm_filler_prefetch(UTTERANCE)
        p = s._filler_prefetch
        assert p["q"] is not None, "arm 必须创建预取合成队列"
        assert p["text"] == UTTERANCE
        assert p["phrase"], "arm 必须选定填充词短语"
        assert p["task"] is not None and not p["task"].done()
        await asyncio.sleep(0.05)
        assert not p["q"].empty(), "DummyTTS 应已产出 PCM 预取块"
    asyncio.run(run())


def test_arm_gates_disabled():
    async def run():
        s, _ = _make_session()
        cfg = get_config()
        saved = cfg._config.get("voice", {}).get("filler_enabled")
        cfg._config.setdefault("voice", {})["filler_enabled"] = False
        try:
            s._arm_filler_prefetch(UTTERANCE)
        finally:
            cfg._config["voice"]["filler_enabled"] = saved
        assert s._filler_prefetch["q"] is None
    asyncio.run(run())


def test_arm_gates_speaking():
    async def run():
        s, _ = _make_session()
        s._speaking = True
        s._arm_filler_prefetch(UTTERANCE)
        assert s._filler_prefetch["q"] is None
    asyncio.run(run())


def test_arm_gates_turn_active():
    async def run():
        s, _ = _make_session()
        s._turn_active = True
        s._arm_filler_prefetch(UTTERANCE)
        assert s._filler_prefetch["q"] is None
    asyncio.run(run())


def test_arm_gates_cooldown():
    async def run():
        s, _ = _make_session()
        s._last_filler_at = _now()
        s._arm_filler_prefetch(UTTERANCE)
        assert s._filler_prefetch["q"] is None, "冷却窗内不得重新 arm"
    asyncio.run(run())


def test_arm_skips_noise_text():
    async def run():
        s, _ = _make_session()
        assert _is_voice_noise("嗯啊咳")
        s._arm_filler_prefetch("嗯啊咳")
        assert s._filler_prefetch["q"] is None
    asyncio.run(run())


def test_arm_skips_stop_command():
    async def run():
        s, _ = _make_session()
        s._arm_filler_prefetch("停下")
        assert s._filler_prefetch["q"] is None
    asyncio.run(run())


def test_arm_rearm_on_text_change():
    """arm-once 守卫（A4.9 I1）：watchdog 只在无存活预取时调用 arm；长话语
    中后续 partial 不得 re-arm（每次 re-arm = 一次被取消的计费 TTS 调用）。"""
    async def run():
        s, _ = _make_session()
        s._arm_filler_prefetch("我想要")
        first = s._filler_prefetch
        # watchdog 侧的 arm-once 条件（与 _eot_watchdog 内的判断同一表达式）
        would_rearm = s._filler_prefetch.get("q") is None
        assert would_rearm is False, "存活预取必须抑制 re-arm"
        assert s._filler_prefetch is first, "存活预取在文本生长时保持（内容无关短语）"
    asyncio.run(run())


# ---------- convert: flush 时的预合成暂存（TTFT 门控后不再立即播） ----------

def test_convert_holds_pending_for_ttft_gate():
    """2026-09-13：convert 只暂存 pending（携带预合成队列），不立即入队/发事件/
    盖冷却——是否播放由生成开始后的 TTFT watch 决定（快轮零填充词）。"""
    async def run():
        s, ws = _make_session()
        s._arm_filler_prefetch(UTTERANCE)
        q = s._filler_prefetch["q"]
        phrase = s._filler_prefetch["phrase"]
        await s._convert_filler_prefetch(UTTERANCE)
        assert _tts_items(s) == [], "convert 不得立即入队"
        assert len(_filler_events(ws)) == 0, "convert 不得立即发 filler 事件"
        assert s._last_filler_at == 0.0, "convert 不得提前盖冷却戳"
        assert s._pending_filler["phrase"] == phrase
        assert s._pending_filler["q"] is q, "pending 必须携带预合成队列"
    asyncio.run(run())


def test_convert_does_not_require_text_match():
    """填充词与内容无关：flush 文本通常比 arm 文本长（partials 持续生长），
    严格相等匹配会丢掉绝大多数 convert（A4.9 I1）。"""
    async def run():
        s, ws = _make_session()
        s._arm_filler_prefetch("我想要")
        await s._convert_filler_prefetch("我想要吃火锅")
        assert s._pending_filler["phrase"], "文本生长后的 flush 仍可转换"
        assert s._filler_prefetch["q"] is None, "转换后预取必须清空"
        assert len(_filler_events(ws)) == 0
    asyncio.run(run())


def test_convert_noise_flush_drops():
    """convert 对 FLUSHED 文本复检噪声门（arm 后被 continue 的垃圾文本不得口播）。"""
    async def run():
        s, ws = _make_session()
        s._arm_filler_prefetch(UTTERANCE)
        assert _is_voice_noise("嗯啊咳")
        await s._convert_filler_prefetch("嗯啊咳")
        assert _tts_items(s) == [], "噪声 flush 不得转换"
        assert s._filler_prefetch["q"] is None
        assert _filler_events(ws) == []
    asyncio.run(run())


def test_convert_stopword_flush_drops():
    async def run():
        s, ws = _make_session()
        s._arm_filler_prefetch(UTTERANCE)
        await s._convert_filler_prefetch("停下")
        assert _tts_items(s) == [], "停止词 flush 不得转换"
        assert s._filler_prefetch["q"] is None
    asyncio.run(run())


def test_convert_blocked_when_speaking_drops():
    async def run():
        s, ws = _make_session()
        s._arm_filler_prefetch(UTTERANCE)
        s._speaking = True
        await s._convert_filler_prefetch(UTTERANCE)
        assert _tts_items(s) == [], "非空闲状态不得转换"
        assert s._filler_prefetch["q"] is None
    asyncio.run(run())


def test_convert_without_arm_is_noop():
    async def run():
        s, ws = _make_session()
        await s._convert_filler_prefetch(UTTERANCE)
        assert _tts_items(s) == []
        assert _filler_events(ws) == []
    asyncio.run(run())


# ---------- fallback：_speak_filler 冷却 + 时序 ----------

def test_fallback_suppressed_within_cooldown():
    async def run():
        s, ws = _make_session()
        s._last_filler_at = _now()
        await s._speak_filler()
        assert _tts_items(s) == [], "冷却窗内 fallback 不得入队"
        assert _filler_events(ws) == []
    asyncio.run(run())


def test_fallback_fires_and_sets_cooldown():
    async def run():
        s, ws = _make_session()
        await s._speak_filler()
        items = _tts_items(s)
        assert len(items) >= 1, "fallback 必须入队填充词（短语可能按标点拆多段）"
        assert all(it["aux"] is True for it in items)
        assert all(it["text"] for it in items)
        assert len(_filler_events(ws)) == 1
        assert s._last_filler_at > 0
    asyncio.run(run())


def test_handle_user_turn_does_not_play_filler_synchronously():
    """TTFT 门控后，填充词不再在 turn 处理时立即入队（旧「filler 先于 persist」
    契约随机制退役）——是否播放由生成路径的 `_filler_ttft_watch` 决定。"""
    async def run():
        s, ws = _make_session()
        order = []

        async def rec_persist(role, content, **kw):
            order.append("persist")

        async def fake_intent(text, history=None):
            return {"should_respond": False, "needs_tools": False, "reason": "test"}

        async def fake_gen(text, prefetched=None):
            await asyncio.sleep(30)

        async def fake_rollback(text):
            pass

        def fake_recall(*a, **k):
            pass

        s._persist_message = rec_persist
        s._classify_intent = fake_intent
        s._generate_and_speak_cancellable = fake_gen
        s._rollback_last_user_message = fake_rollback
        s._start_memory_recall = fake_recall

        await asyncio.wait_for(s._handle_user_turn(UTTERANCE), timeout=10)
        assert "persist" in order, "turn 必须正常持久化"
        assert not [e for e in ws.json_events if e.get("event") == "filler"], \
            "turn 处理阶段不得同步播填充词"
        assert _tts_items(s) == [], "turn 处理阶段不得同步入队填充词"
    asyncio.run(run())


# ---------- config ----------

def test_config_filler_min_gap_default():
    cfg = get_config()
    assert cfg.voice_filler_min_gap_seconds == 3.0
