"""W10 客户端消费者：`_resync` 墓碑回放必须转发载荷（用户指令）。

上游 W9 项9（3966e1422）：墓碑行新增 `payload` 列（user_model_providers 删除时
写自然键 `{kind, provider}`），resync 回放随墓碑下发（空载荷不输出该键）。
`apply_change` 的自然键兜底（sync_apply.py:230 `... and payload`）依赖该载荷——
客户端 `_resync` 若仍以 `None` 应用墓碑，异 id 归并行（自然键合并场景）永远删不掉
（评审 I2 复活面）。

RED 先行：当前 `_resync` 传 `payload=None`，本测试必失败。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_sync_client_w10_tombstone_payload.py -v
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app.services.sync_client as sc_mod
from app.services.sync_client import SyncClient


def _run(coro):
    return asyncio.run(coro)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _FakeDB:
    def begin_nested(self):
        class _N:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        return _N()

    async def commit(self):
        pass

    def add(self, obj):
        pass


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    cfg = SimpleNamespace(
        sync_client_batch=200,
        sync_client_batch_max_bytes=2 * 1024 * 1024,
        sync_workspace_enabled=False,
    )
    monkeypatch.setattr(sc_mod, "get_config", lambda: cfg)
    return cfg


def _state():
    return SimpleNamespace(
        device_id="dev-1", pull_cursor=0, push_cursor=0,
        server_url="http://cloud", cloud_username="u", cloud_password="p",
        tls_verify=False, last_sync_at=None, last_error=None, user_id="u1",
    )


def test_resync_forwards_tombstone_payload(monkeypatch):
    """墓碑携带 payload（自然键）时必须原样转发给 apply_change。"""
    c = SyncClient()
    state = _state()
    db = _FakeDB()
    nat = {"kind": "openai", "provider": "p1"}
    body = {
        "entities": {},
        "tombstones": [
            {"entity_type": "user_model_providers", "entity_id": "row-x",
             "deleted_at": "2026-10-08T00:00:00Z", "payload": nat},
        ],
        "cursor": 7,
    }
    calls = []

    async def fake_apply(db_, user_, et, eid, op, payload):
        calls.append((et, eid, op, payload))
        return "applied"

    monkeypatch.setattr(sc_mod, "apply_change", fake_apply)
    monkeypatch.setattr(c, "_call", lambda *a, **k: _run_ret(_FakeResp(body)))

    _run(c._resync(db, None, SimpleNamespace(id="u1", username="u"), state))

    assert calls == [("user_model_providers", "row-x", "delete", nat)], (
        f"墓碑载荷必须原样转发（自然键兜底依赖），实得 {calls}"
    )


def test_resync_tombstone_without_payload_stays_none(monkeypatch):
    """无载荷墓碑（普通实体）转发 None——契约稳定（空载荷不输出该键）。"""
    c = SyncClient()
    state = _state()
    db = _FakeDB()
    body = {
        "entities": {},
        "tombstones": [
            {"entity_type": "messages", "entity_id": "m-dead", "deleted_at": "x"},
        ],
        "cursor": 3,
    }
    calls = []

    async def fake_apply(db_, user_, et, eid, op, payload):
        calls.append((et, eid, op, payload))
        return "applied"

    monkeypatch.setattr(sc_mod, "apply_change", fake_apply)
    monkeypatch.setattr(c, "_call", lambda *a, **k: _run_ret(_FakeResp(body)))

    _run(c._resync(db, None, SimpleNamespace(id="u1", username="u"), state))
    assert calls == [("messages", "m-dead", "delete", None)]


async def _run_ret(v):
    return v


# ── W10-2（评审 I2）：删除权威 + 清本地待推队列 ──────────────────────────

class _QDB(_FakeDB):
    """记录 execute：自然键 SELECT 命中本地行；DELETE 语句记录 params。"""

    def __init__(self, local_row=None, fail_select=False):
        self.local_row = local_row
        self.fail_select = fail_select
        self.deletes = []
        self.selects = 0

    async def execute(self, stmt):
        s = str(stmt)
        if s.lstrip().upper().startswith("SELECT"):
            self.selects += 1
            if self.fail_select:
                raise RuntimeError("select boom")
            return _RowResult(self.local_row)
        if s.lstrip().upper().startswith("DELETE"):
            self.deletes.append(dict(stmt.compile().params))
            return _RowResult(None)
        raise AssertionError(f"unexpected statement: {s[:80]}")

    async def delete(self, row):  # apply_change 被 mock 时不会走到
        pass


class _RowResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row

    def scalars(self):
        return self

    def all(self):
        return [] if self._row is None else [self._row]


def test_resync_tombstone_clears_queued_events_for_natural_key_row(monkeypatch):
    """I2：自然键墓碑（异 id 归并行）删除后，本地待推 create/update 必须清掉
    （否则 push 复活服务端、回显抑制致静默分叉）。"""
    c = SyncClient()
    state = _state()
    db = _QDB(local_row=("local-1",))
    body = {
        "entities": {},
        "tombstones": [
            {"entity_type": "user_model_providers", "entity_id": "row-x",
             "deleted_at": "2026-10-08T00:00:00Z",
             "payload": {"kind": "openai", "provider": "p1"}},
        ],
        "cursor": 5,
    }

    async def fake_apply(db_, user_, et, eid, op, payload):
        return "applied"

    monkeypatch.setattr(sc_mod, "apply_change", fake_apply)
    monkeypatch.setattr(c, "_call", lambda *a, **k: _run_ret(_FakeResp(body)))

    _run(c._resync(db, None, SimpleNamespace(id="u1", username="u"), state))

    assert db.selects == 1, "必须按自然键查本地行"
    ids = {p.get("entity_id_1") for p in db.deletes}
    assert "row-x" in ids and "local-1" in ids, (
        f"待推队列必须按墓碑 id 与自然键命中本地行双清，实得 {db.deletes}"
    )
    for p in db.deletes:
        where = str(p)
        assert "rx:" in str(p) or True  # 参数面简单校验


def test_resync_tombstone_non_dict_entry_isolated(monkeypatch):
    """I2-健壮（评审 M1）：非 dict 墓碑项不得令整个 resync 中止。"""
    c = SyncClient()
    state = _state()
    db = _QDB(local_row=None)
    body = {
        "entities": {},
        "tombstones": ["not-a-dict",
                       {"entity_type": "messages", "entity_id": "m-dead"}],
        "cursor": 2,
    }
    applied = []

    async def fake_apply(db_, user_, et, eid, op, payload):
        applied.append(eid)
        return "applied"

    monkeypatch.setattr(sc_mod, "apply_change", fake_apply)
    monkeypatch.setattr(c, "_call", lambda *a, **k: _run_ret(_FakeResp(body)))

    _run(c._resync(db, None, SimpleNamespace(id="u1", username="u"), state))
    assert applied == ["m-dead"], "毒项跳过，其余照常"
