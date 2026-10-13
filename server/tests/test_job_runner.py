"""T3 D-重 — durable job runner（criteria 4/5）。

测试面（docs/PLAN_durable_execution_wave.md T3）：
- 幂等：同 idempotency_key 二次 submit 返回同 job
- 状态机 cancel 四态回执：queued→acknowledged+cancelled / running→requested /
  终态→too_late / store 故障→failed
- 真实子进程生命周期：submit→step() 轮→succeeded + exit_code=0 + run.log 内容
  + 事件 WAL seq 连续
- reconcile：exit_code 落盘→finalize；无 exit_code 且 pid 死→unknown
- 日志游标增量不重复；产物 sha256 登记

Hermetic：JobStore 用内存实现（dict）；子进程/文件系统用 tmp 目录真实执行。
"""
import asyncio
import hashlib
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class MemoryJobStore:
    def __init__(self):
        self.jobs = {}
        self.events = {}
        self.artifacts = {}
        self.fail = False

    async def get(self, job_id):
        if self.fail:
            raise RuntimeError("store down")
        cur = self.jobs.get(job_id)
        return dict(cur) if cur else None  # 副本：与 SqlJobStore._to_dict 同语义（防别名突变）

    async def get_by_key(self, key):
        if self.fail:
            raise RuntimeError("store down")
        if not key:
            return None
        for j in self.jobs.values():
            if j.get("idempotency_key") == key:
                return j
        return None

    async def save(self, job):
        if self.fail:
            raise RuntimeError("store down")
        self.jobs[job["id"]] = dict(job)

    async def save_cas(self, job, expected_state):
        cur = self.jobs.get(job["id"])
        if cur is None or cur.get("state") != expected_state:
            return False
        if self.fail:
            raise RuntimeError("store down")
        self.jobs[job["id"]] = dict(job)
        return True

    async def next_seq(self, job_id):
        return len(self.events.get(job_id, [])) + 1

    async def append_event(self, job_id, seq, event_type, payload):
        self.events.setdefault(job_id, []).append(
            {"seq": seq, "event_type": event_type, "payload": payload}
        )

    async def list_events(self, job_id):
        return list(self.events.get(job_id, []))

    async def add_artifact(self, job_id, artifact):
        self.artifacts.setdefault(job_id, []).append(artifact)

    async def list_artifacts(self, job_id):
        return list(self.artifacts.get(job_id, []))

    async def list_jobs(self, user_id, limit):
        # store 契约：user_id=="*" = 全扫（JobRunner._running_rows 的哨兵）
        js = [j for j in self.jobs.values() if user_id == "*" or j["user_id"] == user_id]
        return js[-limit:]

    async def lease_next(self, owner, now, lease_seconds):
        for j in self.jobs.values():
            if j["state"] == "queued":
                j["state"] = "leased"
                j["lease_owner"] = owner
                return dict(j)
        return None


def _runner(tmp_path, store=None):
    from app.services.job_runner_service import JobRunner

    return JobRunner(store=store or MemoryJobStore(), jobs_root=str(tmp_path))


def test_submit_idempotent_same_key(tmp_path):
    from app.services.durable_types import JOB_STATE_QUEUED

    async def main():
        r = _runner(tmp_path)
        spec = {"kind": "shell", "command": "true", "workdir": str(tmp_path)}
        j1 = await r.submit(user_id="u1", source="tool", spec=spec, idempotency_key="k1")
        j2 = await r.submit(user_id="u1", source="tool", spec=spec, idempotency_key="k1")
        j3 = await r.submit(user_id="u1", source="tool", spec=spec, idempotency_key=None)
        return j1, j2, j3

    j1, j2, j3 = asyncio.run(main())
    assert j1["id"] == j2["id"], "同幂等键必须返回既有作业"
    assert j1["state"] == JOB_STATE_QUEUED
    assert j3["id"] != j1["id"]


def test_cancel_receipts(tmp_path):
    async def main():
        r = _runner(tmp_path)
        spec = {"kind": "shell", "command": "true", "workdir": str(tmp_path)}
        jq = await r.submit(user_id="u1", source="tool", spec=spec)
        rq = await r.cancel(jq["id"])
        jq2 = await r.poll(jq["id"])

        jt = await r.submit(user_id="u1", source="tool", spec=spec)
        row_t = await r.store.get(jt["id"])
        row_t["state"] = "running"  # 走合法生命周期 queued→…→running，再终态结算
        await r.store.save(row_t)
        await r._finalize(await r.store.get(jt["id"]), "succeeded", exit_code=0)
        rt = await r.cancel(jt["id"])

        jr = await r.submit(user_id="u1", source="tool", spec=spec, idempotency_key="run1")
        # 手工置 running（模拟已被 worker 领走）
        row = await r.store.get(jr["id"])
        row["state"] = "running"
        await r.store.save(row)
        rr = await r.cancel(jr["id"])
        jr2 = await r.poll(jr["id"])
        return rq, jq2, rt, rr, jr2

    rq, jq2, rt, rr, jr2 = asyncio.run(main())
    assert rq["cancel_state"] == "acknowledged"
    assert jq2["state"] == "cancelled"
    assert rt["cancel_state"] == "too_late"
    assert rr["cancel_state"] == "requested"
    assert jr2["cancel_state"] == "requested"
    assert jr2["cancel_requested_at"] is not None


def test_cancel_store_failure_returns_failed(tmp_path):
    async def main():
        store = MemoryJobStore()
        r = _runner(tmp_path, store)
        spec = {"kind": "shell", "command": "true", "workdir": str(tmp_path)}
        j = await r.submit(user_id="u1", source="tool", spec=spec)
        store.fail = True
        return await r.cancel(j["id"])

    receipt = asyncio.run(main())
    assert receipt["cancel_state"] == "failed"


def test_worker_step_runs_real_subprocess(tmp_path):
    async def main():
        store = MemoryJobStore()
        r = _runner(tmp_path, store)
        spec = {"kind": "shell", "command": "sleep 0.2; echo hi", "workdir": str(tmp_path / "work")}
        j = await r.submit(user_id="u1", source="terminal", spec=spec, idempotency_key="run-hi")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            await r.step()
            row = await r.store.get(j["id"])
            if row["state"] in ("succeeded", "failed", "cancelled", "unknown"):
                break
            await asyncio.sleep(0.05)
        row = await r.store.get(j["id"])
        events = await r.store.list_events(j["id"])
        chunk, _ = await r.logs(j["id"], 0)
        return row, events, chunk

    row, events, chunk = asyncio.run(main())
    assert row["state"] == "succeeded", f"state={row['state']} err={row.get('error')}"
    assert row["exit_code"] == 0
    assert "hi" in chunk
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(seqs) + 1)), f"事件 seq 必须连续: {seqs}"
    types = [e["event_type"] for e in events]
    assert "JobSubmitted" in types and "JobStarted" in types and "JobFinished" in types


def test_logs_cursor_increment_no_repeat(tmp_path):
    async def main():
        r = _runner(tmp_path)
        spec = {"kind": "shell", "command": "printf 'abc'", "workdir": str(tmp_path / "w")}
        j = await r.submit(user_id="u1", source="tool", spec=spec)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            await r.step()
            if (await r.store.get(j["id"]))["state"] == "succeeded":
                break
            await asyncio.sleep(0.05)
        c1, off1 = await r.logs(j["id"], 0)
        c2, off2 = await r.logs(j["id"], off1)
        return c1, off1, c2, off2

    c1, off1, c2, off2 = asyncio.run(main())
    assert "abc" in c1
    assert c2 == "", f"同偏移不应重复返回: {c2!r}"
    assert off2 == off1


def test_reconcile_finalize_and_unknown(tmp_path):
    from app.services.job_runner_service import JobRunner
    from app.services.durable_types import make_key  # noqa: F401  (Hub 锚)

    async def main():
        store = MemoryJobStore()
        r = JobRunner(store=store, jobs_root=str(tmp_path), owner_id="worker-reconcile")
        spec = {"kind": "shell", "command": "true", "workdir": str(tmp_path)}
        # 作业 A：running + exit_code 落盘 + pid 死 → finalize
        ja = await r.submit(user_id="u1", source="tool", spec=spec, idempotency_key="rec-a")
        da = r.job_dir(ja["id"])
        da.mkdir(parents=True, exist_ok=True)
        (da / "exit_code").write_text("0")
        row_a = await store.get(ja["id"])
        row_a.update(state="running", pid=99999999, log_path=str(da / "run.log"))
        await store.save(row_a)
        # 作业 B：running + 无 exit_code + pid 死 → unknown
        jb = await r.submit(user_id="u1", source="tool", spec=spec, idempotency_key="rec-b")
        db_dir = r.job_dir(jb["id"])
        db_dir.mkdir(parents=True, exist_ok=True)
        row_b = await store.get(jb["id"])
        row_b.update(state="running", pid=99999999, log_path=str(db_dir / "run.log"))
        await store.save(row_b)

        summary = await r.reconcile_on_startup()
        return await store.get(ja["id"]), await store.get(jb["id"]), summary

    a, b, summary = asyncio.run(main())
    assert a["state"] == "succeeded" and a["exit_code"] == 0
    assert b["state"] == "unknown"
    assert summary.get("finalized", 0) >= 1 and summary.get("unknown", 0) >= 1


def test_artifact_sha256_registered(tmp_path):
    async def main():
        r = _runner(tmp_path)
        work = tmp_path / "artwork"
        work.mkdir()
        payload = b"hello-artifact"
        (work / "out.bin").write_bytes(payload)
        spec = {"kind": "shell", "command": "true", "workdir": str(work)}
        j = await r.submit(user_id="u1", source="tool", spec=spec)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            await r.step()
            if (await r.store.get(j["id"]))["state"] == "succeeded":
                break
            await asyncio.sleep(0.05)
        return await r.artifacts(j["id"]), payload

    arts, payload = asyncio.run(main())
    by_name = {a["path"].split("/")[-1]: a for a in arts}
    assert "out.bin" in by_name
    assert by_name["out.bin"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert by_name["out.bin"]["bytes"] == len(payload)


def test_submit_validates_spec(tmp_path):
    async def main():
        r = _runner(tmp_path)
        try:
            await r.submit(user_id="u1", source="tool", spec={"kind": "bogus", "command": "x"})
            return "no-error"
        except ValueError:
            return "value-error"

    assert asyncio.run(main()) == "value-error"
