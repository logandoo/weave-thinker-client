"""SqlJobStore.save insert 路径回归（2026-09-30 terminal DurableJob id 双传事故）。

A4.8 RED→GREEN：构造 DurableJob 时 id 同时走显式 kwarg 与 **_ins 必炸
TypeError: got multiple values for keyword argument 'id'。
用假 AsyncSessionLocal（惯例同 test_memory_batch_b_fixes._Sess）直击构造层。
"""
import asyncio
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _job(job_id="job-1"):
    return {
        "id": job_id,
        "user_id": "u1",
        "source": "terminal",
        "spec": {"kind": "terminal", "command": "ls"},
        "idempotency_key": None,
        "state": "queued",
        "attempts": 0,
        "lease_owner": None,
        "lease_until": None,
        "pid": None,
        "log_path": None,
        "exit_code": None,
        "result_digest": None,
        "cancel_requested_at": None,
        "cancel_state": None,
        "error": None,
        "timeout_seconds": 0.0,
        "created_at": datetime.utcnow().isoformat(),
        "started_at": None,
        "finished_at": None,
    }


class _Res:
    def scalars(self):
        return self

    def first(self):
        return None

    def scalar_one(self):
        return 0


class _Sess:
    """捕获 add() 落库行的假会话（insert 路径：get→None）。"""

    added = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, model, key):
        return None

    def add(self, row):
        _Sess.added = row

    async def commit(self):
        pass

    async def execute(self, stmt, params=None):
        return _Res()


def test_save_insert_no_duplicate_id(monkeypatch):
    import app.db.database as dbmod
    from app.services.job_runner_service import SqlJobStore

    monkeypatch.setattr(dbmod, "AsyncSessionLocal", lambda: _Sess())
    store = SqlJobStore()
    job = _job("job-abc")

    asyncio.run(store.save(job))  # RED: TypeError: DurableJob() got multiple values for keyword argument 'id'

    row = _Sess.added
    assert row is not None, "insert 路径必须落库一行"
    assert row.id == "job-abc", "落库行 id 必须等于提交生成的 job id"
    assert json.loads(row.spec) == {"kind": "terminal", "command": "ls"}
    assert row.user_id == "u1" and row.state == "queued"


def test_save_cas_update_skips_id(monkeypatch):
    """save_cas 的 update 回写同样不得把 id 当普通字段 setattr（主键保护）。

    A4.9 评审 A#5/B#6：必须真实调用 save_cas（不是 save）——save_cas 分支
    单独有 ("spec","id") 剔除，零覆盖即 mutation 逃逸口。
    """
    import app.db.database as dbmod
    from app.services.job_runner_service import SqlJobStore

    existing = _job("job-cas")
    existing["state"] = "running"

    class _Row:
        pass

    row = _Row()
    for k, v in existing.items():
        if k != "spec":
            setattr(row, k, v)
    row.spec = json.dumps(existing["spec"])

    class _Res:
        def scalars(self):
            return self

        def first(self):
            return row

    class _CasSess(_Sess):
        async def get(self, model, key):
            return row

        async def execute(self, stmt, params=None):
            return _Res()

    monkeypatch.setattr(dbmod, "AsyncSessionLocal", lambda: _CasSess())
    store = SqlJobStore()
    job = dict(existing)
    job["state"] = "succeeded"
    job["id"] = "job-cas-mutated"  # 恶意/漂移 id：CAS update 路径绝不可覆盖主键

    ok = asyncio.run(store.save_cas(job, expected_state="running"))

    assert ok is True, "expected_state 匹配必须成交"
    assert row.id == "job-cas", "save_cas 不得 setattr 覆盖主键 id"
    assert row.state == "succeeded", "状态更新必须生效"

    # 不匹配分支：expected_state 不符 → False 且行不被改写
    row2 = _Row()
    for k, v in existing.items():
        if k != "spec":
            setattr(row2, k, v)
    row2.spec = json.dumps(existing["spec"])
    row = row2  # execute().first() 闭包读取

    ok2 = asyncio.run(store.save_cas(dict(existing, state="cancelled"),
                                     expected_state="succeeded"))
    assert ok2 is False, "expected_state 不匹配必须拒绝"
    assert row2.state == "running", "拒绝时行不得被改写"
