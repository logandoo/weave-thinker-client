"""W4-4 长任务出口（记忆/长程 v2，2026-10-23，判据 #21）。

判据 #21：检测「多步 + 交付物 + 持续 ≥N 轮」长任务 → 建议升级死磕模式
（需用户确认）。默认 shadow：只产出建议文本/记录，不自动升级。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_long_task_exit.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _msgs(n, *, tool_kinds=0, text="今天天气怎么样"):
    out = []
    for i in range(n):
        out.append({"role": "user" if i % 2 == 0 else "assistant",
                    "content": text if i == 0 else f"{text} {i}"})
    if tool_kinds:
        kinds = ["workspace_write", "process", "web_search", "memory", "chart"]
        for i in range(min(tool_kinds, len(kinds))):
            out.append({"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": kinds[i]}}]})
    return out


class TestDetectLongTask:
    def test_multi_step_with_tools(self):
        from app.services.long_task_exit_service import detect_long_task

        res = detect_long_task(_msgs(10, tool_kinds=3))
        assert res is not None
        assert res["suggested"] is True
        assert res["turn_count"] == 10
        assert res["signals"], "命中必须给信号面"
        assert any(s.startswith("tool_kinds=") for s in res["signals"])
        assert "reason" in res and res["reason"]

    def test_deliverable_signal_without_tools(self):
        from app.services.long_task_exit_service import detect_long_task

        res = detect_long_task(_msgs(10, text="帮我写一份项目部署方案"))
        assert res is not None
        assert any(s.startswith("deliverable=") for s in res["signals"])

    def test_small_talk_returns_none(self):
        from app.services.long_task_exit_service import detect_long_task

        assert detect_long_task(_msgs(6, text="今天天气怎么样")) is None
        # 轮数够但既无工具多样度也无交付物信号
        assert detect_long_task(_msgs(12, text="闲聊一下")) is None

    def test_boundary_turns(self):
        from app.services.long_task_exit_service import detect_long_task

        assert detect_long_task(_msgs(8, tool_kinds=3)) is not None, "恰好 8 轮=命中边界"
        assert detect_long_task(_msgs(7, tool_kinds=3)) is None, "7 轮不足 min_turns"
        assert detect_long_task(_msgs(10, tool_kinds=2)) is None, "工具种类 2 < min_steps=3"
        assert detect_long_task([]) is None
        assert detect_long_task(None) is None

    def test_custom_thresholds(self):
        from app.services.long_task_exit_service import detect_long_task

        assert detect_long_task(_msgs(4, tool_kinds=2),
                                min_turns=4, min_steps=2) is not None
        assert detect_long_task(_msgs(3, tool_kinds=2),
                                min_turns=4, min_steps=2) is None


class TestFormatSuggestion:
    def test_mentions_deathmatch_and_confirmation(self):
        from app.services.long_task_exit_service import (
            detect_long_task, format_upgrade_suggestion)

        res = detect_long_task(_msgs(10, tool_kinds=3))
        s = format_upgrade_suggestion(res)
        assert "死磕" in s
        assert "确认" in s, "升级必须用户确认（shadow：不自动升级）"
        assert res["reason"] in s

    def test_shadow_no_auto_upgrade_wording(self):
        from app.services.long_task_exit_service import format_upgrade_suggestion

        s = format_upgrade_suggestion({"suggested": True, "reason": "多步长任务：轮次 10≥8",
                                       "turn_count": 10, "signals": ["turns=10"]})
        assert "自动升级" not in s or "不会自动升级" in s
