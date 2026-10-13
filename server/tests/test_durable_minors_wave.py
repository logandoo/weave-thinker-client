"""R6 遗留项收口波 — 8 条 Deferred Minors 的行为回归。

覆盖（docs/PLAN… 之外的记忆清单 fix_durable_execution_wave_20260922.md）：
1. read_log_chunk 有界读（不重复、不丢字节）
2. _norm_dt ISO→datetime 显式规范化
3. _SIDE_EFFECT_TOOLS 补全（memory/browser/delegate/schedule/skill/pdf）
4. 台账 failed→pending 原子复活（CAS；并发窗口不覆盖他人 done）
5. job_submit workdir realpath 禁闭（符号链接逃逸拒绝）
6. JobLogOffset 事件随日志消费落 WAL
7. checkpoint 字节口径上限（CJK 精确）
"""
import asyncio
import importlib.util
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).parent / fname)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_TJR = _load("tjr_minors", "test_job_runner.py")
MemoryJobStore = _TJR.MemoryJobStore


def test_log_chunk_bounded_no_loss(tmp_path):
    from app.services.job_runner_service import read_log_chunk

    payload = ("x" * 999 + "\n").encode() * 350  # ~350KB
    p = tmp_path / "big.log"
    p.write_bytes(payload)
    off, total, reads = 0, b"", 0
    while True:
        chunk, off2 = read_log_chunk(p, off, max_chunk=100_000)
        if not chunk:
            break
        assert off2 > off, "游标必须前进"
        total += chunk.encode("utf-8")
        off = off2
        reads += 1
        assert reads < 10, "有界读不应无限循环"
    assert len(total) == len(payload), "字节不丢"
    assert reads >= 3, f"应分多次有界读: {reads}"


def test_norm_dt():
    from datetime import datetime
    from app.services.job_runner_service import _norm_dt

    d = _norm_dt("2026-09-22T10:00:00")
    assert isinstance(d, datetime)
    now = datetime.utcnow()
    assert _norm_dt(now) is now
    assert _norm_dt(None) is None
    assert _norm_dt("not-a-date") is None


def test_side_effect_tools_extended():
    from app.services.agent_loop import AgentLoop

    s = AgentLoop._SIDE_EFFECT_TOOLS
    for name in ("workspace_write", "workspace_edit", "notes", "terminal", "process",
                 "execute_code", "job_submit", "memory", "background_task", "schedule",
                 "delegate_task", "skill_run_script", "skill_manage", "pdf_export",
                 "tts_synthesize", "workspace_snapshot", "job_cancel",
                 "browser_navigate", "browser_click", "browser_type",
                 "browser_press", "browser_execute_js"):
        assert name in s, f"缺 {name}"


class _LedgerStore:
    def __init__(self):
        self.rows = {}

    async def get(self, key):
        return self.rows.get(key)

    async def insert(self, record):
        key = record["idempotency_key"]
        if key in self.rows:
            return False
        self.rows[key] = dict(record)
        return True

    async def update(self, key, status, result_ref=None):
        r = self.rows.get(key)
        if r:
            r["status"] = status
            r["result_ref"] = result_ref

    async def update_if(self, key, expected_status, status):
        r = self.rows.get(key)
        if r is None or r.get("status") != expected_status:
            return False
        r["status"] = status
        r["result_ref"] = None
        return True


class _RacyLedgerStore(_LedgerStore):
    async def update_if(self, key, expected_status, status):
        return False  # 并发窗口内状态已被他人推进


def test_ledger_begin_atomic_resurrect():
    from app.services import side_effect_ledger as sel

    async def main():
        s = _LedgerStore()
        await s.insert({"idempotency_key": "agent_task:t:1:workspace_write:c1",
                        "status": "failed", "result_ref": "boom"})
        k1 = await sel.begin(s, "agent_task", "t", 1, "workspace_write", "c1")
        st1 = (await s.get("agent_task:t:1:workspace_write:c1"))["status"]
        s2 = _RacyLedgerStore()
        await s2.insert({"idempotency_key": "agent_task:t:1:workspace_write:c1",
                         "status": "failed", "result_ref": "boom"})
        k2 = await sel.begin(s2, "agent_task", "t", 1, "workspace_write", "c1")
        return k1, st1, k2

    k1, st1, k2 = asyncio.run(main())
    assert k1 == "agent_task:t:1:workspace_write:c1" and st1 == "pending"
    assert k2 is None, "CAS 失败（他人已推进）必须不执行"


def test_job_submit_workdir_symlink_rejected(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.tools import job_tools
    from app.services.job_runner_service import JobRunner
    from app.core.config import get_config

    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, ws / "link")
    runner = JobRunner(store=MemoryJobStore(), jobs_root=str(tmp_path / "jobs"))
    monkeypatch.setattr(job_tools, "_runner", lambda: runner)
    monkeypatch.setattr(type(get_config()), "workspace_root", property(lambda self: ws))

    async def main():
        return json.loads(await job_tools.job_submit(
            {"kind": "shell", "command": "true", "workdir": "link"},
            user=SimpleNamespace(id="u1"), workspace_path=str(ws)))

    r = asyncio.run(main())
    assert "error" in r and "超出" in r["error"], f"符号链接逃逸必须拒绝: {r}"


def test_log_offset_event_emitted(tmp_path):
    from app.services.job_runner_service import JobRunner

    async def main():
        store = MemoryJobStore()
        r = JobRunner(store=store, jobs_root=str(tmp_path))
        j = await r.submit(user_id="u1", source="shell",
                           spec={"kind": "shell", "command": "printf 'hello'",
                                 "workdir": str(tmp_path / "w")})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            await r.step()
            if (await store.get(j["id"]))["state"] == "succeeded":
                break
            await asyncio.sleep(0.05)
        c1, off1 = await r.logs(j["id"], 0)
        c2, _off2 = await r.logs(j["id"], off1)
        events = await store.list_events(j["id"])
        return c1, c2, [e["event_type"] for e in events]

    c1, c2, types = asyncio.run(main())
    assert "hello" in c1 and c2 == ""
    assert types.count("JobLogOffset") == 1, f"仅实际消费才记（无重复）: {types}"


def test_checkpoint_byte_cap_cjk():
    from app.services.task_checkpoint import build_checkpoint

    msgs = [{"role": "user", "content": "中" * 100} for _ in range(5)]  # ~300B/条
    cp = build_checkpoint(cursor=1, messages=msgs, budget_used=1, max_bytes=1000)
    raw = json.dumps(cp, ensure_ascii=False).encode("utf-8")
    assert len(raw) <= 1500, f"字节口径上限失效: {len(raw)}"
    assert cp.get("dropped_messages", 0) >= 1
    assert cp["messages"], "至少保留一条"


def test_job_submit_disabled_returns_error(tmp_path, monkeypatch):
    """禁用态守卫：enabled=false 时 job_submit 明确拒绝，绝不静默排队。"""
    from types import SimpleNamespace
    from app.tools import job_tools
    from app.services.job_runner_service import JobRunner
    from app.core.config import get_config

    runner = JobRunner(store=MemoryJobStore(), jobs_root=str(tmp_path / "jobs"))
    monkeypatch.setattr(job_tools, "_runner", lambda: runner)
    monkeypatch.setattr(type(get_config()), "agent_durable_jobs_enabled", property(lambda self: False))

    async def main():
        return json.loads(await job_tools.job_submit(
            {"kind": "shell", "command": "true"},
            user=SimpleNamespace(id="u1"), workspace_path=str(tmp_path)))

    r = asyncio.run(main())
    assert "error" in r and "禁用" in r["error"], r


def test_runner_submit_disabled_raises(tmp_path, monkeypatch):
    """兜底守卫：直连 JobRunner.submit 在禁用态同样 fail-closed（覆盖 durable 工具路径）。"""
    from app.services.job_runner_service import JobRunner
    from app.core.config import get_config

    runner = JobRunner(store=MemoryJobStore(), jobs_root=str(tmp_path))
    monkeypatch.setattr(type(get_config()), "agent_durable_jobs_enabled", property(lambda self: False))

    async def main():
        try:
            await runner.submit(user_id="u1", source="tool",
                                spec={"kind": "shell", "command": "true"})
            return "no-error"
        except ValueError as exc:
            return str(exc)

    assert "disabled" in asyncio.run(main())


def test_log_chunk_cjk_boundary_no_corruption(tmp_path):
    """R6 复审 N1：分片边界不得产生替换符；字节零丢失。（ASCII 测试看不见。）"""
    from app.services.job_runner_service import read_log_chunk

    text = "中文日志" * 100  # 每字 3 字节 = 1200B；max_chunk=100 不落在码点边界
    p = tmp_path / "cjk.log"
    p.write_bytes(text.encode("utf-8"))
    off, out = 0, ""
    for _ in range(50):
        chunk, off2 = read_log_chunk(p, off, max_chunk=100)
        assert "\ufffd" not in chunk, f"边界损坏: {chunk!r}"
        out += chunk
        if not chunk and off2 >= p.stat().st_size:
            break
        assert off2 > off, "游标必须前进（否则死循环）"
        off = off2
    assert out == text, f"内容不一致（丢/多字节）: {len(out)} vs {len(text)}"


def test_sqljobstore_datetime_normalized_all_branches():
    """R6 复审阻塞项静态哨兵：insert/update/CAS 三处都必须 _norm_dt（CI 用 Memory
    store 看不见 Sql 分支——此哨兵防回归；_DT_FIELDS 与模型列对齐）。"""
    import inspect
    from app.services.job_runner_service import SqlJobStore, _DT_FIELDS

    src = inspect.getsource(SqlJobStore.save) + inspect.getsource(SqlJobStore.save_cas)
    assert src.count("_norm_dt(") >= 3, "insert/update/CAS 三分支都必须规范化 datetime"
    assert set(_DT_FIELDS) == {"created_at", "started_at", "finished_at",
                               "lease_until", "cancel_requested_at"}
