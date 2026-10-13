"""Toolcall liveness/heartbeat tests (2026-09-03, conv 827a6f78 wave).

Background: tool_call_timeout_seconds=1800 (both deployments) means a hung
VLM socket burned 30 minutes twice before the hard timeout fired. This suite
pins the new liveness machinery:

- per-tool hard-cap overrides (vision_interpret -> 3600s)
- per-tool STALL detection (progress-aware kill: real long tasks keep
  reporting/produce chunks and are never killed; silence >= stall => stuck)
- vision_interpret streaming VLM call reporting per-chunk progress
- code_execution reporting progress on subprocess output chunks
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


# ── tool_progress primitives ──────────────────────────────────────────

def test_tool_stalled_error_exists():
    from app.services.tool_progress import ToolStalledError  # noqa: F401


def test_report_tool_progress_noop_without_context():
    """report_tool_progress must be safe outside a guarded call (subagents,
    deathmatch, scheduler paths where no tracker is installed)."""
    from app.services.tool_progress import report_tool_progress
    report_tool_progress("noop")  # must not raise


def test_tracker_seconds_since_progress():
    from app.services.tool_progress import ToolProgressTracker
    t = ToolProgressTracker()
    time.sleep(0.05)
    assert t.seconds_since_progress() >= 0.04
    t.report("tick")
    assert t.seconds_since_progress() < 0.04


def test_run_with_liveness_progressing_task_survives():
    """A tool that keeps reporting progress is NEVER killed by the stall
    watchdog (the user requirement: real long tasks must survive)."""
    from app.services.tool_progress import report_tool_progress, run_with_liveness

    async def tool():
        out = []
        for i in range(6):
            await asyncio.sleep(0.06)
            report_tool_progress(f"chunk {i}")
            out.append(i)
        return "".join(str(x) for x in out)

    result = asyncio.run(run_with_liveness(tool(), stall_timeout=0.15, check_interval=0.04))
    assert result == "012345"


def test_run_with_liveness_silent_task_killed_fast():
    """A tool with NO progress beyond the stall window is killed with
    ToolStalledError well before a would-be 1800s hard cap."""
    from app.services.tool_progress import ToolStalledError, run_with_liveness

    started = time.monotonic()

    async def tool():
        await asyncio.sleep(30)  # would outlive the test if not killed
        return "done"

    with pytest.raises(ToolStalledError) as ei:
        asyncio.run(run_with_liveness(tool(), stall_timeout=0.2, check_interval=0.05))
    elapsed = time.monotonic() - started
    assert "no progress" in str(ei.value)
    assert elapsed < 5.0, f"stall kill took {elapsed:.1f}s — watchdog not firing early"


def test_run_with_liveness_disabled_when_stall_zero():
    """stall_timeout=0 disables the watchdog entirely (D-2: opt-in)."""
    from app.services.tool_progress import run_with_liveness

    async def tool():
        await asyncio.sleep(0.3)
        return "ok"

    assert asyncio.run(run_with_liveness(tool(), stall_timeout=0, check_interval=0.05)) == "ok"


def test_resolve_tool_deadlines():
    from app.services.tool_progress import resolve_tool_deadlines

    hard, stall = resolve_tool_deadlines(
        "vision_interpret",
        hard_default=1800.0,
        hard_overrides={"vision_interpret": 3600.0},
        stall_overrides={"vision_interpret": 600.0, "code_execution": 300.0},
    )
    assert hard == 3600.0
    assert stall == 600.0
    hard2, stall2 = resolve_tool_deadlines(
        "terminal", hard_default=1800.0,
        hard_overrides={"vision_interpret": 3600.0}, stall_overrides={},
    )
    assert hard2 == 1800.0 and stall2 == 0.0


# ── config surface ────────────────────────────────────────────────────

def test_config_liveness_properties_exist():
    from app.core.config import Config

    cfg = Config.__new__(Config)  # bypass file loading; exercise property defaults
    object.__setattr__(cfg, "_config", {})
    assert cfg.agent_tool_loop_tool_timeout_overrides == {}
    assert cfg.agent_tool_loop_stall_timeout_overrides == {}
    assert cfg.agent_tool_loop_progress_check_interval_seconds == 10.0
    object.__setattr__(
        cfg, "_config",
        {"agent": {"tool_loop": {
            "tool_call_timeout_overrides": {"vision_interpret": 3600},
            "tool_call_stall_timeout_overrides": {"vision_interpret": 600},
            "tool_call_progress_check_interval_seconds": 5,
        }}},
    )
    assert cfg.agent_tool_loop_tool_timeout_overrides == {"vision_interpret": 3600.0}
    assert cfg.agent_tool_loop_stall_timeout_overrides == {"vision_interpret": 600.0}
    assert cfg.agent_tool_loop_progress_check_interval_seconds == 5.0


# ── vision_interpret streaming liveness ───────────────────────────────

class _FakeStreamLLM:
    """Mimics LLMService.stream_chat_structured with delayed content chunks."""

    def __init__(self, reports: list):
        self._reports = reports

    async def stream_chat_structured(self, messages, **kwargs):
        assert kwargs.get("temperature") == 0
        for i, piece in enumerate(["草莓", "品种", "特性说明"]):
            await asyncio.sleep(0.05)
            yield {"type": "content", "data": piece}
            assert self._reports, f"no progress reported by chunk {i}"
        yield {"type": "done", "data": {"finish_reason": "stop"}}


def test_vision_interpret_streams_and_reports(monkeypatch, tmp_path):
    import base64
    from app.services import tool_progress
    from app.tools import vision_interpret as vi

    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG fake")

    async def _fake_resolve(assistant=None):
        return type("EP", (), {
            "base_url": "http://x", "api_key": "k", "model_name": "vlm-m",
            "with_overrides": lambda self, **kw: self,
        })(), "configured"

    monkeypatch.setattr(vi, "_resolve_vision", _fake_resolve)

    reports: list = []
    real_report = tool_progress.report_tool_progress
    monkeypatch.setattr(vi, "report_tool_progress", lambda note: reports.append(note))

    class _Factory:
        @staticmethod
        def build_llm_service(ep):
            return _FakeStreamLLM(reports)

    from app.model_gateway import factory as gw_factory
    monkeypatch.setattr(gw_factory, "build_llm_service", _Factory.build_llm_service)

    result = asyncio.run(vi.vision_interpret(
        {"image_path": str(img), "question": "描述"},
        workspace_path=str(tmp_path),
    ))
    payload = __import__("json").loads(result)
    assert payload["success"] is True
    assert payload["interpretation"] == "草莓品种特性说明"
    assert reports, "vision_interpret must report progress (heartbeat) during streaming"


# ── code_execution progress instrumentation ──────────────────────────

def test_code_execution_reports_progress_on_output(monkeypatch, tmp_path):
    """Subprocess output chunks are a liveness signal: _read_all reports."""
    from app.services import code_execution_service as ces

    reports: list = []
    monkeypatch.setattr(ces, "report_tool_progress", lambda note: reports.append(note))

    async def run_once():
        svc = ces.CodeExecutionService()
        return await svc.execute_python(
            "print('hello line')\nprint('world line')",
            cwd=str(tmp_path),
        )

    result = asyncio.run(run_once())
    assert result is not None
    assert reports, "code_execution must report progress while draining subprocess output"


# ── agent_loop wiring (A4.9 Minor-5: the wiring itself is tested) ─────

def test_agent_loop_wiring_resolves_overrides(monkeypatch):
    """_tool_liveness_coro must consult the per-tool override maps and return
    (guarded_coro, hard_cap); unconfigured tools fall back to the default."""
    import types
    from app.services import agent_loop as al_mod
    from app.services.tool_progress import ToolStalledError, run_with_liveness  # noqa: F401

    class _Dummy:
        _tool_call_timeout = 1800.0

    stub = types.SimpleNamespace(
        agent_tool_loop_tool_timeout_overrides={"vision_interpret": 3600.0},
        agent_tool_loop_stall_timeout_overrides={"vision_interpret": 600.0},
        agent_tool_loop_progress_check_interval_seconds=10.0,
    )
    monkeypatch.setattr(al_mod, "config", stub, raising=False)

    async def _noop():
        return "x"

    loop = object.__new__(al_mod.AgentLoop)
    loop._tool_call_timeout = 1800.0
    # D-4: 有存活信号的工具（vision_interpret stall=600）外层无绝对墙钟（None）
    guarded, outer = loop._tool_liveness_coro("vision_interpret", _noop())
    assert outer is None, f"liveness tool outer timeout must be None, got {outer}"
    import inspect
    assert inspect.iscoroutine(guarded) or inspect.iscoroutinefunction(guarded)
    # 未配置 stall 的工具保持绝对墙钟
    guarded2, hard2 = loop._tool_liveness_coro("terminal", _noop())
    assert hard2 == 1800.0
    # guarded coro completes and returns the tool result
    import asyncio
    assert asyncio.run(guarded2) == "x"


def test_progress_resets_the_hard_cap_itself(monkeypatch):
    """用户语义修正（2026-09-03）：进展重置的是上限本身，不是仅停滞计时。
    一个持续上报进展的工具必须能存活超过名义硬上限（wall-clock 0.35s）；
    旧实现（外层 wait_for 硬上限从起点计）会在 0.35s 杀掉它。"""
    import types
    from app.services import agent_loop as al_mod

    class _Dummy:
        _tool_call_timeout = 0.35  # 名义硬上限（wall-clock 口径下的杀线）

    stub = types.SimpleNamespace(
        agent_tool_loop_tool_timeout_overrides={},
        agent_tool_loop_stall_timeout_overrides={"code_execution": 5.0},
        agent_tool_loop_progress_check_interval_seconds=0.02,
    )
    monkeypatch.setattr(al_mod, "config", stub, raising=False)
    loop = object.__new__(al_mod.AgentLoop)
    loop._tool_call_timeout = 0.35

    async def tool():
        from app.services.tool_progress import report_tool_progress
        result = []
        for i in range(12):  # 总时长 ~0.6s > 名义硬上限 0.35s
            await asyncio.sleep(0.05)
            report_tool_progress(f"step {i}")
            result.append(i)
        return sum(result)

    guarded, outer_timeout = loop._tool_liveness_coro("code_execution", tool())
    # 语义断言：有存活信号的工具，外层不再有绝对墙钟上限（None = 无 wall-clock）
    assert outer_timeout is None, (
        f"有进展信号的工具外层仍挂了绝对墙钟上限 {outer_timeout}s —— 进展无法重置上限"
    )
    import asyncio
    assert asyncio.run(guarded) == sum(range(12))


def test_no_signal_tool_keeps_wall_clock_cap(monkeypatch):
    """无存活信号的工具（未配置停滞检测）保持绝对墙钟上限不变。"""
    import types
    from app.services import agent_loop as al_mod

    stub = types.SimpleNamespace(
        agent_tool_loop_tool_timeout_overrides={},
        agent_tool_loop_stall_timeout_overrides={},
        agent_tool_loop_progress_check_interval_seconds=10.0,
    )
    monkeypatch.setattr(al_mod, "config", stub, raising=False)
    loop = object.__new__(al_mod.AgentLoop)
    loop._tool_call_timeout = 1800.0

    async def tool():
        return "x"

    guarded, outer_timeout = loop._tool_liveness_coro("terminal", tool())
    assert outer_timeout == 1800.0


# ── audit_reset buffer sync (conv 827a6f78 turn-B: silent-QC gap) ─────

def test_stream_buffer_audit_reset_drops_draft_text():
    """audit_reset 必须重置 buffer.content 并裁剪最后工具卡之后的 text 时间线
    项（保留 reasoning_step 与工具卡）——否则 resume replay 与 done 气泡
    会复活全部被审计打回的草稿。"""
    import asyncio
    from app.services.stream_buffer import ConversationBuffer

    async def drive():
        buf = ConversationBuffer(conversation_id="t", user_id=1)
        await buf.append({"tool_call": {"call_id": "c1", "name": "code_execution", "arguments": {}}})
        await buf.append({"content": "被拒绝的草稿文本" * 10})
        await buf.append({"reasoning_segment": {"title": "💭 思考过程", "content": "保留的思考"}})
        await buf.append({"audit_reset": True})
        return buf

    buf = asyncio.run(drive())
    assert buf.content == "", f"content must reset, got {buf.content[:50]!r}"
    types = [it.get("type") for it in buf.display_sequence]
    assert "tool_call" in types
    assert "reasoning_step" in types, "reasoning preserved (A4 parity)"
    assert "text" not in types, f"draft text items must be dropped, got {types}"
