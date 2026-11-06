"""RED (2026-10-22, 60 生产 conv 5c89e092 症状1)：SqlJobStore.save 新建作业必抛
`DurableJob() got multiple values for keyword argument 'id'`。

根因：insert 分支 `DurableJob(id=job["id"], spec=..., **_ins)`，而 `_ins`
来自 `fields = {k: v for k, v in job.items() if k != "spec"}`——`id` 仍在
`_ins` 里，与显式 `id=` 撞键。既有测试全用 MemoryJobStore，Sql 分支从未被
真实执行（CI hermetic 约束）——本测试用伪 AsyncSession 钉死 insert/update
两条分支的行为契约，无需真实 DB。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _job_dict(**over):
    """与 SqlJobStore._to_dict 全字段同构的作业 dict（生产形状）。"""
    d = {
        "id": "job-test-1", "user_id": "u1", "source": "terminal",
        "spec": {"kind": "shell", "command": "sleep 1"},
        "idempotency_key": None, "state": "queued", "attempts": 0,
        "lease_owner": None, "lease_until": None, "pid": None,
        "log_path": None, "exit_code": None, "result_digest": None,
        "cancel_requested_at": None, "cancel_state": None, "error": None,
        "timeout_seconds": 0.0,
        "created_at": "2026-10-22T00:00:00", "started_at": None,
        "finished_at": None,
    }
    d.update(over)
    return d


class _FakeDB:
    """最小 AsyncSession 替身：get/add/commit——insert 分支的 TypeError 在
    DurableJob 构造时（db.add 之前）抛出，因此替身无需 DB 语义。"""

    def __init__(self, row=None):
        self._row = row
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, model, pk):
        return self._row

    async def execute(self, stmt):
        class _Res:
            def __init__(self, row):
                self._row = row

            def scalars(self):
                class _S:
                    def __init__(self, row):
                        self._row = row

                    def first(self):
                        return self._row
                return _S(self._row)
        return _Res(self._row)

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        pass


def test_insert_new_job_never_raises_duplicate_id(monkeypatch):
    """60 生产错误原文：durable terminal/job_submit 新建作业必崩——修后应成功。"""
    from app.db import database as db_mod
    from app.services.job_runner_service import SqlJobStore

    fake = _FakeDB(row=None)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", lambda: fake)
    store = SqlJobStore()

    asyncio.run(store.save(_job_dict()))  # bug: TypeError multiple values for 'id'

    assert len(fake.added) == 1
    row = fake.added[0]
    assert row.id == "job-test-1"
    assert row.user_id == "u1"
    assert row.source == "terminal"
    assert json.loads(row.spec) == {"kind": "shell", "command": "sleep 1"}


def test_insert_maps_all_dt_fields(monkeypatch):
    """insert 分支 datetime 字段须规范化（既有 _norm_dt 契约不破）。"""
    from app.db import database as db_mod
    from app.services.job_runner_service import SqlJobStore

    fake = _FakeDB(row=None)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", lambda: fake)
    store = SqlJobStore()

    asyncio.run(store.save(_job_dict(
        id="job-test-2",
        lease_until="2026-10-22T01:00:00",
        started_at="2026-10-22T00:30:00",
    )))

    row = fake.added[0]
    from datetime import datetime
    assert isinstance(row.created_at, datetime)
    assert isinstance(row.lease_until, datetime)
    assert isinstance(row.started_at, datetime)
    assert row.finished_at is None


def test_update_existing_job_keeps_id(monkeypatch):
    """update 分支：既有行 setattr 路径不回写 id（id 不可变）。"""
    from app.db import database as db_mod
    from app.db.database import DurableJob
    from app.services.job_runner_service import SqlJobStore

    row = DurableJob(id="job-test-3", user_id="u1", source="terminal",
                     spec=json.dumps({"kind": "shell"}), state="queued")
    fake = _FakeDB(row=row)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", lambda: fake)
    store = SqlJobStore()

    asyncio.run(store.save(_job_dict(id="job-test-3", state="running", pid=4242)))

    assert row.id == "job-test-3"
    assert row.state == "running"
    assert row.pid == 4242
    assert json.loads(row.spec) == {"kind": "shell", "command": "sleep 1"}


def test_save_cas_never_writes_id(monkeypatch):
    """re-review Minor-1：CAS 分支也不得回写主键（同值 setattr 无害，但语义
    必须与 save() 的「主键不可变」一致——dict 形态漂移时不得改 id）。"""
    from app.db import database as db_mod
    from app.db.database import DurableJob
    from app.services.job_runner_service import SqlJobStore

    row = DurableJob(id="row-id", user_id="u1", source="terminal",
                     spec=json.dumps({}), state="running")
    fake = _FakeDB(row=row)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", lambda: fake)
    store = SqlJobStore()

    job = _job_dict(id="drifted-id", state="succeeded")
    ok = asyncio.run(store.save_cas(job, "running"))
    assert ok is True
    assert row.id == "row-id", "主键被 CAS 回写篡改"
    assert row.state == "succeeded"
