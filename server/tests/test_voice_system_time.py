"""TDD tests — 语音系统提示词当前时间注入（voice 与 chat 对齐）。

chat 模式在 agent_service.py 动态 section 注入
`当前时间: {now} (北京时间, {weekday})`；voice 此前完全没有时钟注入，
"几点了/周几"只能瞎编。wave-2（2026-08-25）在 `_system_messages()`
每次调用时注入同一格式的时间行。
"""
from datetime import datetime

from unittest.mock import patch

import asyncio

from app.services import voice_service as vs


class _DummyTTS:
    async def stream_tts(self, text, style_instruction=None, timeout=60.0):
        yield b"\x00" * 960


class _DummyWS:
    async def send_bytes(self, data):
        pass

    async def send_text(self, text):
        pass


def _make_session():
    s = vs.VoiceDuplexSession(_DummyWS(), None, None, conversation_id=None)
    s.tts = _DummyTTS()
    return s


def test_system_messages_contain_current_time():
    async def run():
        s = _make_session()
        s._system_prompt = lambda: "BASE"
        now = datetime(2026, 8, 25, 22, 59, 58)
        fake = type("FakeDT", (), {"now": staticmethod(lambda *a, **k: now)})
        with patch.object(vs, "datetime", fake):
            msgs = s._system_messages()
        content = msgs[0]["content"]
        assert content.startswith("BASE"), "注入必须追加在系统提示词之后"
        expected = f"当前时间: {now.strftime('%Y年%m月%d日 %H:%M:%S')} (北京时间, {now.strftime('%A')})"
        assert expected in content, f"时间行缺失或格式不符: {expected!r}\n{content[-300:]}"
        assert msgs[0]["role"] == "system"
    asyncio.run(run())


def test_system_messages_time_refreshes_per_call():
    async def run():
        s = _make_session()
        s._system_prompt = lambda: "BASE"
        clock = {"t": datetime(2026, 8, 25, 10, 0, 0)}

        def fake_now():
            return clock["t"]

        fake = type("FakeDT", (), {"now": staticmethod(lambda *a, **k: fake_now())})
        with patch.object(vs, "datetime", fake):
            first = s._system_messages()[0]["content"]
            clock["t"] = datetime(2026, 8, 25, 10, 30, 0)
            second = s._system_messages()[0]["content"]
        assert "10:00:00" in first
        assert "10:30:00" in second, "每次调用必须取实时时钟（长会话不陈旧）"
    asyncio.run(run())
