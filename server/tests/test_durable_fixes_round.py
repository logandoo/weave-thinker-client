"""A4.9 R1 修复轮回归测试（C2/I1/I2/I7/I8 + C1 工具元数据）。

覆盖：
- C2 归属门：非属主 poll/logs/cancel/artifacts 一律 not found（不泄露存在性）
- I1 幂等键跨用户拒绝
- I7 子壳退出回执：`exit 0`/`exit 7` 必落 exit_code（旧花括号组会漏回执）
- I8 别名同键：path/file_path 指向同一文件时分波串行
- C1 job_submit：permission_key 元数据 + workdir 禁闭 + 拦截命令（_runner 打桩为内存版）

Hermetic：本地 MemoryJobStore；子进程/文件系统走 tmp 真实路径。
"""
import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class MemoryJobStore:
    def __init__(self):
        self.jobs = {}
        self.events = {}
        self.artifacts = {}

    async def get(self, job_id):
        cur = self.jobs.get(job_id)
        return dict(cur) if cur else None  # 副本语义（同 SqlJobStore._to_dict）

    async def get_by_key(self, key):
        if not key:
            return None
        for j in self.jobs.values():
            if j.get("idempotency_key") == key:
                return j
        return None

    async def save(self, job):
        self.jobs[job["id"]] = dict(job)

    async def save_cas(self, job, expected_state):
        cur = self.jobs.get(job["id"])
        if cur is None or cur.get("state") != expected_state:
            return False
        self.jobs[job["id"]] = dict(job)
        return True

    async def next_seq(self, job_id):
        return len(self.events.get(job_id, [])) + 1

    async def append_event(self, job_id, seq, event_type, payload):
        self.events.setdefault(job_id, []).append({"seq": seq, "event_type": event_type})

    async def list_events(self, job_id):
        return list(self.events.get(job_id, []))

    async def add_artifact(self, job_id, artifact):
        self.artifacts.setdefault(job_id, []).append(artifact)

    async def list_artifacts(self, job_id):
        return list(self.artifacts.get(job_id, []))

    async def list_jobs(self, user_id, limit):
        js = [j for j in self.jobs.values() if user_id == "*" or j["user_id"] == user_id]
        return js[-limit:]

    async def lease_next(self, owner, now, lease_seconds):
        for j in self.jobs.values():
            if j["state"] == "queued":
                j["state"] = "leased"
                j["lease_owner"] = owner
                return dict(j)
        return None


def _runner(tmp_path):
    from app.services.job_runner_service import JobRunner

    return JobRunner(store=MemoryJobStore(), jobs_root=str(tmp_path))


def test_ownership_denied_for_other_user(tmp_path):
    async def main():
        r = _runner(tmp_path)
        spec = {"kind": "shell", "command": "true", "workdir": str(tmp_path)}
        j = await r.submit(user_id="u1", source="tool", spec=spec)
        out = {}
        for name, call in {
            "poll": lambda: r.poll(j["id"], user_id="u2"),
            "logs": lambda: r.logs(j["id"], 0, user_id="u2"),
            "artifacts": lambda: r.artifacts(j["id"], user_id="u2"),
        }.items():
            try:
                await call()
                out[name] = "leaked"
            except KeyError:
                out[name] = "denied"
        out["cancel"] = (await r.cancel(j["id"], user_id="u2"))["cancel_state"]
        out["owner_poll"] = (await r.poll(j["id"], user_id="u1"))["state"]
        return out

    out = asyncio.run(main())
    assert out["poll"] == "denied" and out["logs"] == "denied" and out["artifacts"] == "denied"
    assert out["cancel"] in ("failed", "too_late"), f"cancel 不应泄露/执行: {out['cancel']}"
    assert out["owner_poll"] == "queued"


def test_idempotency_key_cross_user_rejected(tmp_path):
    async def main():
        r = _runner(tmp_path)
        spec = {"kind": "shell", "command": "true", "workdir": str(tmp_path)}
        await r.submit(user_id="u1", source="tool", spec=spec, idempotency_key="shared-k")
        try:
            await r.submit(user_id="u2", source="tool", spec=spec, idempotency_key="shared-k")
            return "leaked"
        except ValueError:
            return "rejected"

    assert asyncio.run(main()) == "rejected"


def test_shell_exit_receipt_written(tmp_path):
    async def main():
        r = _runner(tmp_path)
        jobs = {}
        for tag, cmd in (("ok", "exit 0"), ("bad", "exit 7")):
            j = await r.submit(user_id="u1", source="terminal",
                               spec={"kind": "shell", "command": cmd, "workdir": str(tmp_path / tag)})
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                await r.step()
                row = await r.store.get(j["id"])
                if row["state"] in ("succeeded", "failed", "cancelled", "unknown"):
                    break
                await asyncio.sleep(0.05)
            jobs[tag] = await r.store.get(j["id"])
        return jobs

    jobs = asyncio.run(main())
    assert jobs["ok"]["state"] == "succeeded", f"exit 0 必须落回执: {jobs['ok']}"
    assert jobs["ok"]["exit_code"] == 0
    assert jobs["bad"]["state"] == "failed" and jobs["bad"]["exit_code"] == 7


def test_alias_path_same_lock_wave():
    from app.services.workspace_lock_service import partition_write_waves

    def tc(name, cid, **args):
        return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}

    waves = partition_write_waves([
        tc("workspace_edit", "1", path="./a.md"),
        tc("workspace_edit", "2", file_path="a.md"),
    ])
    assert len(waves) == 2, f"别名同文件必须分波串行: {waves}"


def test_job_submit_tool_guardrails(tmp_path, monkeypatch):
    from app.tools import job_tools
    from app.services.job_runner_service import JobRunner
    from app.core.config import get_config

    runner = JobRunner(store=MemoryJobStore(), jobs_root=str(tmp_path / "jobs"))
    monkeypatch.setattr(job_tools, "_runner", lambda: runner)
    user = SimpleNamespace(id="u1")
    kwargs = {"user": user, "workspace_path": str(tmp_path)}

    async def main():
        out = {}
        # 本机 config super_admin_bypass=True 会短路静态校验——测护栏时显式关闭
        monkeypatch.setattr(type(get_config()), "super_admin_bypass", property(lambda self: False))
        # 夹具工作区即 tmp_path（终端越界检查以 workspace_root 为界）
        monkeypatch.setattr(type(get_config()), "workspace_root", property(lambda self: tmp_path))
        # 元数据：与 terminal 同级权限门（C1）
        from app.tools.registry import registry
        entry = registry.get_entry("job_submit")
        out["permission_key"] = entry.permission_key if entry else None
        # workdir 越界拒绝（I2）
        r1 = json.loads(await job_tools.job_submit(
            {"kind": "shell", "command": "true", "workdir": "../evil"}, **kwargs))
        out["escape_blocked"] = "error" in r1
        # 被拦命令（C1）
        r2 = json.loads(await job_tools.job_submit(
            {"kind": "shell", "command": "sudo rm -rf /tmp/x"}, **kwargs))
        out["dangerous_blocked"] = "error" in r2 or "_permission_needed" in r2
        # 正常提交
        r3 = json.loads(await job_tools.job_submit(
            {"kind": "shell", "command": "true", "workdir": "sub"}, **kwargs))
        out["submitted"] = bool(r3.get("job_id"))
        # 归属传递（C2）：冒充他人查询 → not found
        r4 = json.loads(await job_tools.job_status({"job_id": r3["job_id"]},
                                                   user=SimpleNamespace(id="u2")))
        out["cross_user_status"] = "error" in r4
        return out

    out = asyncio.run(main())
    assert out["permission_key"] == "terminal_execution", out
    assert out["escape_blocked"], out
    assert out["dangerous_blocked"], out
    assert out["submitted"], out
    assert out["cross_user_status"], out


class MemoryLedgerStore:
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
        row = self.rows.get(key)
        if row:
            row["status"] = status
            row["result_ref"] = result_ref

    async def update_if(self, key, expected_status, status):
        row = self.rows.get(key)
        if row is None or row.get("status") != expected_status:
            return False
        row["status"] = status
        row["result_ref"] = None
        return True


def test_ledger_failed_allows_retry():
    """A4.9 R2 N4：失败尝试不得被当作成功吞掉——failed → 重试执行；done → 跳过。"""
    from app.services import side_effect_ledger as sel

    async def main():
        store = MemoryLedgerStore()
        k1 = await sel.begin(store, "agent_task", "t5", 1, "workspace_write", "c9")
        await sel.finish(store, k1, "failed", "boom")
        skip_after_fail = await sel.should_skip(store, "agent_task", "t5", 1, "workspace_write", "c9")
        k2 = await sel.begin(store, "agent_task", "t5", 1, "workspace_write", "c9")
        await sel.finish(store, k2, "done", "fixed")
        skip_after_done = await sel.should_skip(store, "agent_task", "t5", 1, "workspace_write", "c9")
        return skip_after_fail, k2, skip_after_done

    skip_after_fail, k2, skip_after_done = asyncio.run(main())
    assert skip_after_fail is False, "failed 记录必须允许重试"
    assert k2 == "agent_task:t5:1:workspace_write:c9"
    assert skip_after_done is True


def test_save_cas_conflict_rejected(tmp_path):
    """A4.9 R2 I5：状态条件写——陈旧行不得覆盖已推进状态。"""
    async def main():
        r = _runner(tmp_path)
        spec = {"kind": "shell", "command": "true", "workdir": str(tmp_path)}
        j = await r.submit(user_id="u1", source="tool", spec=spec)
        row = await r.store.get(j["id"])
        row["state"] = "leased"
        await r.store.save(row)  # 他人推进状态
        stale = await r.store.get(j["id"])
        stale["attempts"] = 9
        ok_conflict = await r._save_cas(stale, "queued")   # 期望 queued → 冲突拒绝
        ok_match = await r._save_cas(stale, "leased")      # 期望 leased → 成功
        return ok_conflict, ok_match, (await r.store.get(j["id"]))["attempts"]

    ok_conflict, ok_match, attempts = asyncio.run(main())
    assert ok_conflict is False and ok_match is True and attempts == 9


def test_canonical_key_path_unifies_forms(tmp_path):
    """A4.9 R2 I8：相对/绝对/含 .. 的同一文件必须同键。"""
    from app.services.workspace_lock_service import canonical_key_path

    ws = str(tmp_path)
    rel = canonical_key_path(ws, "a.md")
    ab = canonical_key_path(ws, str(tmp_path / "a.md"))
    dot = canonical_key_path(ws, "sub/../a.md")
    assert rel == ab == dot, (rel, ab, dot)


def test_preflight_redispatch_with_ledger_dedup(monkeypatch, tmp_path):
    """A4.9 R3：恢复 preflight——已记录合成结果、新调用派发、截断调用合成反馈。"""
    from app.services import side_effect_ledger as sel
    from app.services.agent_worker import AgentWorker
    from app.tools.registry import registry

    ledger = MemoryLedgerStore()
    monkeypatch.setattr(sel, "get_ledger_store", lambda: ledger)
    calls = []

    async def fake_dispatch(name, args, **kwargs):
        calls.append((name, dict(args)))
        return f"ok:{name}"

    monkeypatch.setattr(registry, "dispatch", fake_dispatch)

    tcs = [
        {"id": "c1", "type": "function",
         "function": {"name": "workspace_read", "arguments": '{"path":"a.md"}'}},
        {"id": "c2", "type": "function",
         "function": {"name": "workspace_read", "arguments": '{"path":"b.md"}'}},
        {"id": "c3", "type": "function",
         "function": {"name": "workspace_read", "arguments": '{"path":"c.md"'}},
        {"id": "c4", "type": "function",
         "function": {"name": "workspace_read", "arguments": '{"path": '}},
    ]
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": tcs},
    ]

    async def main():
        k1 = await sel.begin(ledger, "agent_task", "task1", 5, "workspace_read", "c1")
        await sel.finish(ledger, k1, "done", "cached-result")
        worker = AgentWorker()
        out = await worker._preflight_pending_tool_calls(
            messages, task_id="task1", cursor=5, user=None, conversation=None,
            assistant=None, workspace_path=str(tmp_path))
        return out, ledger

    out, ledger = asyncio.run(main())
    tool_msgs = [m for m in out if m.get("role") == "tool"]
    by_id = {m["tool_call_id"]: m["content"] for m in tool_msgs}
    assert len(tool_msgs) == 4, out
    assert by_id["c1"] == "cached-result", "已记录调用必须合成台账结果，不重复派发"
    assert by_id["c2"] == "ok:workspace_read", "未记录调用必须派发"
    assert by_id["c3"] == "ok:workspace_read", "可修复参数（活路径同款 repair）必须派发"
    assert json.loads(by_id["c4"]).get("error"), f"截断参数必须合成错误反馈: {by_id['c4']}"
    dispatched = [c for c in calls if c[0] == "workspace_read"]
    assert ("workspace_read", {"path": "b.md"}) in dispatched
    assert ("workspace_read", {"path": "c.md"}) in dispatched, f"修复后的 c3 应派发: {calls}"
    assert len(dispatched) == 2, f"c1 不派发、c4 不派发: {calls}"
    for cid in ("c3", "c4"):
        skip = asyncio.run(sel.should_skip(ledger, "agent_task", "task1", 5, "workspace_read", cid))
        if cid == "c3":
            assert skip is True, "c3 派发后应记台账"
        else:
            assert skip is False, "截断调用不得记台账（后续可重试）"


def test_mark_drained_status_guard_exists():
    """A4.9 R3 NEW-3：drain 只允许改写 running/claimed（防复活终态）。"""
    import inspect
    from app.services.agent_worker import AgentWorker

    src = inspect.getsource(AgentWorker._mark_drained)
    assert 'status.in_(("running", "claimed"))' in src
    assert "rowcount == 0" in src, "终态跳过需可观测"


def test_preflight_denies_privileged_ops(monkeypatch, tmp_path):
    """A4.9 R4 NEW-2 覆盖：恢复路径同样拒绝后台受限操作（terminal 等）。"""
    from app.services import side_effect_ledger as sel
    from app.services.agent_worker import AgentWorker
    from app.tools.registry import registry

    ledger = MemoryLedgerStore()
    monkeypatch.setattr(sel, "get_ledger_store", lambda: ledger)
    calls = []

    async def fake_dispatch(name, args, **kwargs):
        calls.append(name)
        return "ok"

    monkeypatch.setattr(registry, "dispatch", fake_dispatch)
    tcs = [{"id": "t1", "type": "function",
            "function": {"name": "terminal", "arguments": '{"command":"ls"}'}}]
    messages = [{"role": "assistant", "content": None, "tool_calls": tcs}]

    async def main():
        return await AgentWorker()._preflight_pending_tool_calls(
            messages, task_id="task2", cursor=1, user=None, conversation=None,
            assistant=None, workspace_path=str(tmp_path))

    out = asyncio.run(main())
    tool_msgs = [m for m in out if m.get("role") == "tool"]
    assert len(tool_msgs) == 1 and "denied" in tool_msgs[0]["content"], out
    assert calls == [], "被拒操作不得派发"
    skip = asyncio.run(sel.should_skip(ledger, "agent_task", "task2", 1, "terminal", "t1"))
    assert skip is False, "被拒操作不记台账"


def test_preflight_sanitizes_pending_message_only(monkeypatch, tmp_path):
    """A4.9 R5 NEW-6：多轮恢复只合法化待续消息，旧轮 assistant 不被污染。"""
    from app.services import side_effect_ledger as sel
    from app.services.agent_worker import AgentWorker
    from app.tools.registry import registry

    monkeypatch.setattr(sel, "get_ledger_store", lambda: MemoryLedgerStore())

    async def fake_dispatch(name, args, **kwargs):
        return "ok"

    monkeypatch.setattr(registry, "dispatch", fake_dispatch)
    old1 = {"id": "old1", "type": "function",
            "function": {"name": "workspace_read", "arguments": '{"path":"old.md"}'}}
    new1 = {"id": "new1", "type": "function",
            "function": {"name": "workspace_read", "arguments": '{"path": '}}
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [old1]},
        {"role": "tool", "tool_call_id": "old1", "name": "workspace_read", "content": "done-old"},
        {"role": "assistant", "content": None, "tool_calls": [new1]},
    ]

    async def main():
        return await AgentWorker()._preflight_pending_tool_calls(
            messages, task_id="task3", cursor=2, user=None, conversation=None,
            assistant=None, workspace_path=str(tmp_path))

    out = asyncio.run(main())
    # 旧轮消息保持原样（id 与参数都未被改写）
    assert messages[0]["tool_calls"][0]["id"] == "old1"
    assert json.loads(messages[0]["tool_calls"][0]["function"]["arguments"])["path"] == "old.md"
    # 待续消息的参数被合法化（NEW-6：合法 JSON，不再把非法参数带上 wire）
    pending_args = messages[2]["tool_calls"][0]["function"]["arguments"]
    json.loads(pending_args)  # 不抛=合法
    # 且截断反馈入 tool 消息
    new_msgs = [m for m in out if m.get("role") == "tool" and m.get("tool_call_id") == "new1"]
    assert new_msgs and json.loads(new_msgs[0]["content"]).get("error")
