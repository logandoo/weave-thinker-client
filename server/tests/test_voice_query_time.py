"""TDD tests — 语音 query 层当前时间注入（每轮 user 消息携带 local time）。

2026-08-25 wave 已在 system 层（_system_messages）每轮注入时钟；本 wave
按用户要求把时间标记落到 query 层：LLM-bound messages 的最后一条 user
消息追加时间标记。硬不变式：不得改写 _history 中的 dict（持久化/UI
语义），组装必须实时取钟（显式 UTC+8，对齐 system 层与 chat 模式）。
"""
import asyncio
from datetime import datetime
from unittest.mock import patch

from app.services import voice_service as vs


class _DummyWS:
    async def send_bytes(self, data):
        pass

    async def send_text(self, text):
        pass


def _make_session(history):
    s = vs.VoiceDuplexSession(_DummyWS(), None, None, conversation_id=None)
    s._system_prompt = lambda: "BASE"
    s._history = list(history)
    return s


def test_generation_messages_stamp_last_user_query():
    """主循环形态：user 消息已在 history 末尾 → query 副本带时间标记。"""
    async def run():
        s = _make_session([{"role": "user", "content": "现在几点了"}])
        msgs = s._generation_messages()
        last = msgs[-1]
        assert last["role"] == "user", "最后一条必须是本轮 user query"
        assert last["content"].startswith("现在几点了"), "query 原文必须保留在最前"
        assert "当前时间:" in last["content"], "query 必须携带时间标记"
        assert "北京时间" in last["content"], "时间标记必须显式 UTC+8"
    asyncio.run(run())


def test_generation_messages_with_extra_user_stamps_extra():
    """prefetch 形态：text 尚未入 history，显式追加的 user 文本被标记。"""
    async def run():
        s = _make_session([
            {"role": "user", "content": "旧的一轮"},
            {"role": "assistant", "content": "旧的回答"},
        ])
        msgs = s._generation_messages("帮我查一下明天天气")
        last = msgs[-1]
        assert last["role"] == "user"
        assert last["content"].startswith("帮我查一下明天天气")
        assert "当前时间:" in last["content"], "显式追加的 query 必须被标记"
        # 只标最后一条：历史里的旧 user 消息不加标记
        prev_users = [m for m in msgs[:-1] if m.get("role") == "user"]
        assert prev_users and all("当前时间:" not in m["content"] for m in prev_users), \
            "只有本轮 query 携带标记，历史 user 消息不重复注入"
    asyncio.run(run())


def test_generation_messages_do_not_mutate_history():
    """硬不变式：_history 的 dict 原文与对象身份均不被改写。"""
    async def run():
        history = [{"role": "user", "content": "现在几点了"}]
        s = _make_session(history)
        msgs = s._generation_messages()
        assert s._history[-1]["content"] == "现在几点了", "持久化原文不得变"
        assert s._history[-1] is history[-1], "_history dict 对象身份不得变"
        assert s._history[-1] is not msgs[-1], "LLM 副本必须是新 dict"
    asyncio.run(run())


def test_query_time_refreshes_per_assembly():
    """每次组装实时取钟：时钟推进后新组装的标记时间随之推进。"""
    async def run():
        s = _make_session([{"role": "user", "content": "现在几点了"}])
        clock = {"t": datetime(2026, 9, 6, 10, 0, 0)}

        fake = type("FakeDT", (), {"now": staticmethod(lambda *a, **k: clock["t"])})
        with patch.object(vs, "datetime", fake):
            first = s._generation_messages()[-1]["content"]
            clock["t"] = datetime(2026, 9, 6, 10, 30, 0)
            second = s._generation_messages()[-1]["content"]
        assert "10:00:00" in first
        assert "10:30:00" in second, "每次组装必须实时取钟（感知时间流逝）"
    asyncio.run(run())


def test_system_layer_clock_still_present():
    """system 层时钟保留（2026-08-25 行为不回归）且与 query 标记同格式源。"""
    async def run():
        s = _make_session([{"role": "user", "content": "现在几点了"}])
        msgs = s._generation_messages()
        sys_content = msgs[0]["content"]
        assert sys_content.startswith("BASE")
        assert "当前时间:" in sys_content, "system 层时钟必须保留"
    asyncio.run(run())
