"""TDD tests for voice subagent structured output (JSON schema) + thinking-off
audit — 2026-09-13.

用户要求：语音助理的 subagent 采用 JSON schema 回复；语音助理中所有 LLM 调用
关闭思考。供应商支持不一（DeepSeek 实测 json_schema 400 / json_object OK），
故 `_quick_classify` 走 schema → json_object → off 三级阶梯，被拒档位按 purpose
学习缓存；任何档位下 thinking 都必须关闭（子代理强制，主路径 config 门控）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_voice_subagent_json_schema.py -v
"""
import asyncio

from app.services.voice_service import (
    VoiceDuplexSession,
    _BARGE_IN_RESPONSE_FORMAT,
    _EOT_RESPONSE_FORMAT,
    _INTENT_RESPONSE_FORMAT,
    _INTERJECTION_RESPONSE_FORMAT,
    _MEMORY_INTERJECTION_RESPONSE_FORMAT,
    _looks_like_response_format_error,
)


class _DummyWS:
    def __init__(self):
        self.sent = []
        self.json_events = []

    async def send_bytes(self, data):
        self.sent.append(data)

    async def send_text(self, text):
        self.json_events.append(text)


class _FakeClient:
    base_url = "https://api.deepseek.com/v1"


class _FakeCompleteLLM:
    """Records complete_chat kwargs; rejects configured response_format types."""

    def __init__(self, reject_types=(), fail_first_with=None):
        self.reject_types = set(reject_types)
        self.fail_first_with = fail_first_with
        self.calls = []
        self.client = _FakeClient()

    async def complete_chat(self, messages, **kwargs):
        self.calls.append(kwargs)
        rf = kwargs.get("response_format")
        if self.fail_first_with is not None and len(self.calls) == 1:
            raise RuntimeError(self.fail_first_with)
        if rf is not None and rf.get("type") in self.reject_types:
            raise RuntimeError("This response_format type is unavailable now")
        return '{"should_respond": true, "needs_tools": false, "reason": "ok"}'


def _make_session():
    ws = _DummyWS()
    s = VoiceDuplexSession(ws, None, None, conversation_id=None)

    async def _send_json(ev):
        ws.json_events.append(ev)

    s._send_json = _send_json
    return s, ws


def _is_thinking_off(body) -> bool:
    if not isinstance(body, dict):
        return False
    return (
        body.get("enable_thinking") is False
        or (body.get("thinking") or {}).get("type") == "disabled"
    )


# ---------- 1. 阶梯降级 + 学习 ----------

def test_schema_rejected_downgrades_to_json_object_and_learns():
    async def run():
        s, _ = _make_session()
        fake = _FakeCompleteLLM(reject_types={"json_schema"})
        s._build_llm = lambda purpose="voice": (fake, "m")
        out = await s._quick_classify(
            "sys", "user", "voice.intent", response_format=_INTENT_RESPONSE_FORMAT
        )
        assert "should_respond" in out
        assert [c.get("response_format", {}).get("type") for c in fake.calls] == [
            "json_schema", "json_object",
        ], "被拒后必须降级到 json_object 再试"
        assert s._json_modes["voice.intent"] == "object"
        # 学习生效：第二次调用直接走 json_object，不再试 schema
        fake.calls.clear()
        await s._quick_classify(
            "sys", "user", "voice.intent", response_format=_INTENT_RESPONSE_FORMAT
        )
        assert [c.get("response_format", {}).get("type") for c in fake.calls] == ["json_object"]
    asyncio.run(run())


def test_full_downgrade_to_off_when_provider_rejects_object_too():
    async def run():
        s, _ = _make_session()
        fake = _FakeCompleteLLM(reject_types={"json_schema", "json_object"})
        s._build_llm = lambda purpose="voice": (fake, "m")
        out = await s._quick_classify(
            "sys", "user", "voice.eot", response_format=_EOT_RESPONSE_FORMAT
        )
        assert out
        assert [c.get("response_format", {}).get("type") for c in fake.calls] == [
            "json_schema", "json_object", None,
        ]
        assert s._json_modes["voice.eot"] == "off"
        fake.calls.clear()
        await s._quick_classify(
            "sys", "user", "voice.eot", response_format=_EOT_RESPONSE_FORMAT
        )
        assert fake.calls[0].get("response_format") is None
    asyncio.run(run())


def test_non_rf_error_retries_without_response_format():
    async def run():
        s, _ = _make_session()
        fake = _FakeCompleteLLM(fail_first_with="thinking parameter not supported")
        s._build_llm = lambda purpose="voice": (fake, "m")
        out = await s._quick_classify(
            "sys", "user", "voice.intent", response_format=_INTENT_RESPONSE_FORMAT
        )
        assert out
        assert len(fake.calls) == 2
        assert fake.calls[1].get("response_format") is None
        # 非 RF 错误不得污染学习缓存
        assert "voice.intent" not in s._json_modes
    asyncio.run(run())


def test_rf_detector_tokens():
    assert _looks_like_response_format_error(
        RuntimeError("This response_format type is unavailable now"))
    assert _looks_like_response_format_error(RuntimeError("json_schema not supported"))
    assert not _looks_like_response_format_error(RuntimeError("thinking not supported"))
    # A4.9 Important #2：裸 token / 请求体回显不得误判为 RF 拒绝
    assert not _looks_like_response_format_error(
        RuntimeError("response_format was included in the request"))
    assert not _looks_like_response_format_error(RuntimeError(
        'invalid request: {"response_format": {...}, "thinking": {"type": "disabled"}}'))
    assert not _looks_like_response_format_error(
        RuntimeError("unexpected keyword argument 'thinking' (payload had response_format)"))


# ---------- 2. 五个调用点都带 schema ----------

def test_all_subagent_call_sites_request_structured_output():
    async def run():
        s, _ = _make_session()
        seen = {}

        async def fake(system, user, purpose="voice", response_format=None):
            seen[purpose] = response_format
            return (
                '{"should_respond": true, "needs_tools": false, "reason": "x",'
                ' "complete": true, "verdict": "interrupt", "should_interject": false,'
                ' "emotion": "calm", "interjection_text": "", "action": "none", "line": ""}'
            )

        s._quick_classify = fake
        await s._classify_intent("你好")
        await s._classify_eot("你好")
        await s._classify_barge_in("你好")
        await s._classify_interjection("你好。", [])
        await s._classify_memory_interjection("记忆", "回答", "", "postscript")
        assert set(seen) == {
            "voice.intent", "voice.duplex", "voice.interjection", "voice.memory_interjection",
        }
        for purpose, rf in seen.items():
            assert rf is not None, f"{purpose} 必须请求结构化输出"
            assert rf["type"] == "json_schema"
            assert rf["json_schema"]["strict"] is True
            assert rf["json_schema"]["schema"]["additionalProperties"] is False
    asyncio.run(run())


# ---------- 3. barge-in JSON 解析 + 兼容旧纯词 ----------

def test_barge_in_parses_json_verdict():
    async def run():
        s, _ = _make_session()

        async def fake(system, user, purpose="voice", response_format=None):
            return '{"verdict": "defer"}'

        s._quick_classify = fake
        assert await s._classify_barge_in("补充一句") == "defer"
    asyncio.run(run())


def test_barge_in_accepts_legacy_bare_word():
    async def run():
        s, _ = _make_session()

        async def fake_interrupt(system, user, purpose="voice", response_format=None):
            return "interrupt"

        async def fake_backchannel(system, user, purpose="voice", response_format=None):
            return ""

        s._quick_classify = fake_interrupt
        assert await s._classify_barge_in("停下") == "interrupt"
        s._quick_classify = fake_backchannel
        assert await s._classify_barge_in("嗯嗯") == "backchannel"
    asyncio.run(run())


# ---------- 4. thinking off 审计 ----------

def test_subagent_calls_always_thinking_off():
    async def run():
        s, _ = _make_session()
        fake = _FakeCompleteLLM(reject_types={"json_schema"})
        s._build_llm = lambda purpose="voice": (fake, "m")
        await s._quick_classify(
            "sys", "user", "voice.intent", response_format=_INTENT_RESPONSE_FORMAT
        )
        assert len(fake.calls) == 2
        for call in fake.calls:
            assert _is_thinking_off(call.get("extra_body")), (
                f"子代理每次尝试都必须关思考: {call.get('extra_body')}"
            )
    asyncio.run(run())


class _FakeStreamLLM:
    def __init__(self):
        self.kwargs = None

    async def stream_chat_structured(self, messages, **kwargs):
        self.kwargs = kwargs
        yield {"type": "content", "data": "好"}


def test_main_prefetch_path_thinking_off():
    async def run():
        s, _ = _make_session()
        fake = _FakeStreamLLM()
        s._build_llm = lambda purpose="voice": (fake, "m")
        await s._generate_reply_text("你好")
        assert fake.kwargs is not None
        assert _is_thinking_off(fake.kwargs.get("extra_body"))
    asyncio.run(run())


# ---------- 5. schema 形状 ----------

def test_schema_shapes_declare_required_fields():
    assert _INTENT_RESPONSE_FORMAT["json_schema"]["schema"]["required"] == [
        "should_respond", "needs_tools", "reason",
    ]
    assert _EOT_RESPONSE_FORMAT["json_schema"]["schema"]["required"] == ["complete", "reason"]
    assert _BARGE_IN_RESPONSE_FORMAT["json_schema"]["schema"]["properties"]["verdict"]["enum"] == [
        "interrupt", "defer", "backchannel",
    ]
    assert _INTERJECTION_RESPONSE_FORMAT["json_schema"]["schema"]["required"] == [
        "should_interject", "emotion", "interjection_text", "reason",
    ]
    assert _MEMORY_INTERJECTION_RESPONSE_FORMAT["json_schema"]["schema"]["properties"]["action"]["enum"] == [
        "none", "append", "correct",
    ]
