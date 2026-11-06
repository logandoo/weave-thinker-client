"""TDD: 超时契约修复（2026-11-01，审计 docs/AUDIT_20261101_terminal_timeout_contract.md）

覆盖：
- F1 沙箱子进程默认不缓冲 → 超时被杀时「部分输出」不再静默丢失
  （`_sandbox_env` / `_build_env` 必须带 PYTHONUNBUFFERED=1；端到端跑真实子进程验证）
- F2 同一根因让 execute_code 的存活信号失效（无 chunk → stall watchdog 误杀健康任务）
- F3 逃生口文案：stdout 为空时不得断言「已产生上述部分 stdout」，且必须指向
  `durable=true`（同工具）而不是研究型的 `background_task`
- F5 回执可辨：`killed_by` + `timeout_effective`（不再只有 -1）
- F4 状态口径：`timed_out` 载荷必须判为 error（UI/台账/trace 三处同源）

Run from backend/:  ../.venv/bin/python -m pytest tests/test_terminal_timeout_contract.py -v
"""
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import get_config  # noqa: E402
from app.services.code_execution_service import CodeExecutionService, _build_env  # noqa: E402
from app.services.tool_progress import ToolStalledError, run_with_liveness  # noqa: E402
from app.tools.terminal import _sandbox_env, terminal  # noqa: E402

config = get_config()


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def ws():
    root = Path(config.workspace_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    d = root / ("_timeout_contract_%d_%d" % (os.getpid(), int(time.time() * 1000) % 100000))
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- F1: env


def test_sandbox_env_unbuffers_python(ws):
    env = _sandbox_env("/usr/bin:/bin", str(ws), {})
    assert env.get("PYTHONUNBUFFERED") == "1", "terminal 沙箱必须让 python 子进程不缓冲"


def test_code_exec_env_unbuffers_python(ws):
    env = _build_env(str(ws))
    assert env.get("PYTHONUNBUFFERED") == "1", "execute_code 沙箱必须让 python 子进程不缓冲"


def test_code_exec_keeps_partial_output_without_flush(ws):
    """不写 flush 的 print，在超时被杀后也必须能读到（现场 conv a4d54ec7 的形态）。"""
    code = "import time\nprint('progress 1/3')\nprint('progress 2/3')\ntime.sleep(30)\n"
    r = _run(CodeExecutionService().execute_python(code, cwd=str(ws), timeout=3))
    assert r.timed_out is True
    assert "progress 1/3" in r.stdout, "部分输出丢失: %r" % (r.stdout,)
    assert "progress 2/3" in r.stdout


def test_terminal_keeps_partial_output_without_flush(ws):
    script = ws / "slow_print.py"
    script.write_text(
        "import time\nprint('queued prompt_id: abc')\nprint('waiting...')\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    payload = json.loads(_run(terminal(
        {"command": "python3 %s" % script, "timeout": 5}, workspace_path=str(ws))))
    assert payload["timed_out"] is True
    assert "queued prompt_id: abc" in payload["stdout"], "部分输出丢失: %r" % (payload["stdout"],)


# ------------------------------------------------- F2: liveness must survive


def test_healthy_job_is_not_stalled_without_flush(ws):
    """每秒打印进度（不写 flush）的健康任务，不得被 stall watchdog 误杀。"""
    code = (
        "import time\n"
        "for i in range(12):\n"
        "    print('epoch %d/12' % i)\n"
        "    time.sleep(0.25)\n"
    )

    async def _go():
        return await run_with_liveness(
            CodeExecutionService().execute_python(code, cwd=str(ws), timeout=30),
            stall_timeout=2,
            check_interval=0.5,
        )

    try:
        r = _run(_go())
    except ToolStalledError as exc:  # pragma: no cover - RED 阶段走这里
        pytest.fail("健康任务被 stall 误杀: %s" % exc)
    assert r.return_code == 0, "return_code=%s stdout=%r" % (r.return_code, r.stdout)
    assert len(r.stdout.splitlines()) == 12


# --------------------------------------------------------- F3/F5: 回执契约


def test_terminal_timeout_payload_contract(ws):
    script = ws / "silent_long.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    payload = json.loads(_run(terminal(
        {"command": "python3 %s" % script, "timeout": 5}, workspace_path=str(ws))))

    assert payload["timed_out"] is True
    assert payload["return_code"] == -1  # 向后兼容：-1 仍表示"未正常结束"
    assert payload.get("killed_by") == "SIGKILL"
    assert payload.get("timeout_effective") == pytest.approx(5.0, abs=0.01)

    g = payload["guidance"]
    # 无输出时不得断言"已产生上述部分 stdout 输出"
    assert "已产生上述部分 stdout 输出" not in g
    # 必须给出真正对口的逃生口
    assert "durable=true" in g
    # background_task 只能作为"研究型长任务"提及，不能是本场景建议
    assert "研究" in g


def test_terminal_guidance_with_partial_output_mentions_it(ws):
    script = ws / "chatty_long.py"
    script.write_text(
        "import time\nprint('step 1 done')\ntime.sleep(30)\n", encoding="utf-8")
    payload = json.loads(_run(terminal(
        {"command": "python3 %s" % script, "timeout": 5}, workspace_path=str(ws))))
    assert payload["timed_out"] is True
    assert "step 1 done" in payload["stdout"]
    assert "部分输出" in payload["guidance"]


def test_execute_code_timeout_payload_contract(ws):
    """execute_code 的另一条路径：共用同一个 guidance 构造器（不重复旧文案）。"""
    from app.tools import code_execution as ce
    from app.tools.timeout_guidance import build_timeout_guidance

    g = build_timeout_guidance(
        240.0, False, tool_name="execute_code",
        durable_hint="用**同一个** execute_code 工具的 durable=true 提交持久作业",
    )
    assert "已产生上述部分 stdout 输出" not in g, g[:200]
    assert "durable=true" in g
    assert "研究" in g
    assert "240s" in g
    # 工具层确实改用了共用构造器（而不是各写一份文案）
    assert "build_timeout_guidance" in Path(ce.__file__).read_text(encoding="utf-8")


# --------------------------------------------------- F4: 载荷级失败判定


def test_payload_is_timeout_recognises_only_real_timeouts():
    from app.services.tool_payload_status import payload_is_timeout

    assert payload_is_timeout('{"stdout": "", "timed_out": true, "return_code": -1}') is True
    assert payload_is_timeout('{"stdout": "", "timed_out": true, "return_code": 0}') is False
    assert payload_is_timeout('{"timed_out": false}') is False
    # 工具白名单：非超时工具的普通 JSON 不得误判
    assert payload_is_timeout(
        '{"timed_out": true, "return_code": -1}', "workspace_read") is False
    assert payload_is_timeout('{"stdout": "ok", "return_code": 0}') is False
    # 正文里出现该字样、但不是 JSON 载荷 → 不误判
    assert payload_is_timeout('脚本里写了 "timed_out": true') is False
    assert payload_is_timeout("") is False
    assert payload_is_timeout(None) is False


def test_tool_call_result_payload_error_defaults_false():
    from app.services.agent_loop import ToolCallResult

    tr = ToolCallResult(call_id="c1", name="terminal", arguments={}, result="{}")
    assert tr.error is False and tr.payload_error is False


# ------------------------------------------- I1/I2/I4: 评审补充的回归钉


def test_timeout_envelope_survives_budget_folding(ws):
    """大结果被预算桩折叠后，超时关键字段必须仍然可见（评审 I1）。"""
    from app.services.tool_payload_status import timeout_envelope

    payload = json.dumps({
        "stdout": "x" * 5000,
        "stderr": "",
        "return_code": -1,
        "error": "Command timed out after 120.0s",
        "timed_out": True,
        "killed_by": "SIGKILL",
        "timeout_effective": 120.0,
        "guidance": "用 durable=true 提交持久作业",
    })
    env = timeout_envelope(payload)
    assert "<timeout-envelope>" in env
    assert "durable=true" in env
    assert "timed_out" in env
    # 非超时载荷不生成
    assert timeout_envelope('{"stdout":"ok","return_code":0}') == ""


def test_code_execution_result_reports_real_timeout(ws):
    """M4/I4：回执里的 timeout_effective / killed_by 来自本次实际执行。"""
    r = _run(CodeExecutionService().execute_python(
        "import time\ntime.sleep(30)\n", cwd=str(ws), timeout=3))
    assert r.timed_out is True
    assert r.timeout_effective == pytest.approx(3.0, abs=0.01)
    assert r.killed_by == "SIGTERM+SIGKILL"


def test_payload_is_timeout_rejects_non_str():
    """M2：bytes/int 不得抛 TypeError 把成功工具翻成失败。"""
    from app.services.tool_payload_status import payload_is_timeout

    assert payload_is_timeout(b'{"timed_out": true}') is False
    assert payload_is_timeout(123) is False
    assert payload_is_timeout({"timed_out": True}) is False


class _FakeDB:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


class _FakeLoop:
    """只实现 _execute_single_tool_inner 走到 dispatch 所需的最小表面。"""

    blocked_tools = set()
    delegation_depth = 0
    workspace_path = ""
    vibeweaver_active = False
    deathmatch_manager = None
    _visible_tool_names = None
    _active_tool_names = set()
    _act_gate_approval_callback = None
    _succeeded_call_sigs = set()
    _SIDE_EFFECT_TOOLS = frozenset({"terminal"})

    def session_factory(self):
        return _FakeDB()

    def _skip_guardrails(self):
        return False

    def _strip_internal_tool_args(self, args):
        return args

    def _dm_step_tool_names(self):
        return None

    async def _execute_single_tool_inner(self, *args, **kwargs):
        raise AssertionError("台账 skip 路径不得再派发工具")

    def __getattr__(self, name):
        from unittest.mock import MagicMock
        if name.startswith("_"):
            return MagicMock()
        raise AttributeError(name)


def test_execute_single_tool_marks_timeout_as_payload_error(ws, monkeypatch):
    """I5：payload_is_timeout → ToolCallResult.payload_error 的真实接线。"""
    from app.services import agent_loop_tools as alt
    from app.services.agent_loop import ToolCallResult
    from app.tools.registry import registry

    timeout_payload = json.dumps({
        "stdout": "", "stderr": "", "return_code": -1,
        "timed_out": True, "killed_by": "SIGKILL", "guidance": "…",
    }, ensure_ascii=False)

    async def _dispatch(name, args, **kwargs):
        return timeout_payload

    monkeypatch.setattr(registry, "dispatch", _dispatch)
    monkeypatch.setattr(registry, "get_schema", lambda name: {"parameters": {"type": "object"}})
    monkeypatch.setattr(type(config), "tma_mode", property(lambda self: "off"))

    state = SimpleNamespace(
        _doom_tool_history=[],
        _doom_result_hashes={},
        _succeeded_call_sigs=set(),
        _fetched_urls=set(),
        iterations=1,
        session_id="s1",
        budget=SimpleNamespace(used=0),
    )
    out = _run(alt.AgentToolMixin._execute_single_tool_inner(
        _FakeLoop(), "call_x", "terminal", {"command": "true"},
        None, None, None, None, state,
    ))
    assert isinstance(out, ToolCallResult)
    assert out.error is False
    assert out.payload_error is True

    # SSE 事件必须把这个载荷级失败翻成 error（I5 的另一半）
    from app.services.agent_loop_orchestrator import tool_result_event
    ev = tool_result_event("call_x", "terminal", out)
    assert ev["tool_result"]["error"] is True

    ok = ToolCallResult(call_id="call_y", name="terminal", arguments={},
                        result='{"stdout":"ok","return_code":0}')
    assert tool_result_event("call_y", "terminal", ok)["tool_result"]["error"] is False


from types import SimpleNamespace  # noqa: E402  (供上面的 helper 使用)


def test_persisted_agent_step_carries_error_status():
    """I3：持久化 agent_steps 必须带 status，回放不再依赖前端推断。

    复核 M1：预算折叠后的内容是 <persisted-output> 桩，不能只靠内容嗅探 ——
    优先用本次调用已算好的 error 标记。
    """
    from app.api.chat import _transform_tool_loop_results

    payload = json.dumps({
        "stdout": "", "stderr": "", "return_code": -1,
        "error": "Command timed out after 120.0s", "timed_out": True,
    }, ensure_ascii=False)
    folded = "<persisted-output>...\n</persisted-output>\n<timeout-envelope>{}</timeout-envelope>"
    out = _transform_tool_loop_results([
        {"call_id": "c1", "name": "terminal", "result": payload, "error": True},
        {"call_id": "c2", "name": "terminal",
         "result": '{"stdout":"ok\\n","return_code":0}', "error": False},
        # 折叠过的超时结果：内容已不是 JSON，但 error 标记仍在
        {"call_id": "c3", "name": "terminal", "result": folded, "error": True},
        # 老数据兜底：没有 error 标记时按载荷形状判断
        {"call_id": "", "name": "terminal", "result": payload, "error": False},
    ])
    data = json.loads(out)
    steps = data["agent_steps"]
    assert steps[0]["status"] == "error"
    assert steps[1]["status"] == "completed"
    assert steps[2]["status"] == "error", "折叠后的超时不得退回 completed"
    assert steps[3]["status"] == "error"


def test_ledger_replay_keeps_payload_error(ws, monkeypatch):
    """复核 M2：台账 skip 后的重放结果也要保留载荷级失败（否则 resume 后变回成功）。"""
    from app.services import agent_loop as al
    from app.services import side_effect_ledger as sel

    recorded = json.dumps({
        "stdout": "", "stderr": "", "return_code": -1,
        "error": "Command timed out after 120.0s", "timed_out": True,
    }, ensure_ascii=False)

    async def _skip(*a, **k):
        return True

    async def _get(*a, **k):
        return recorded

    monkeypatch.setattr(sel, "should_skip", _skip)
    monkeypatch.setattr(sel, "get_result", _get)

    state = SimpleNamespace(
        principal=("user", "u1"),
        budget=SimpleNamespace(used=3),
    )
    out = _run(al.AgentLoop._execute_single_tool_ledger(
        _FakeLoop(), "call_z", "terminal", {"command": "true"},
        None, None, None, None, state,
    ))
    assert out.error is False
    assert out.payload_error is True


def test_escape_hatches_are_unbuffered(ws):
    """复核 M5：guidance 让长任务改走 durable/process，这两条链路也必须不缓冲。"""
    from app.services.process_session_service import ProcessSessionManager

    env = ProcessSessionManager()._build_env(str(ws))
    assert env.get("PYTHONUNBUFFERED") == "1"

    from app.services import job_runner_service as jr

    src = Path(jr.__file__).read_text(encoding="utf-8")
    # durable 的 python 分支必须以 -u 启动，否则 job_logs 看不到进度
    assert '-u {shlex.quote(str(script))}' in src
