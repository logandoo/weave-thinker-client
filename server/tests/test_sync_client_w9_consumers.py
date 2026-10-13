"""W9 客户端同步消费者三件（用户指令，2026-10-08）：

1) `_pull` 消费 `resync_required`（8a 客户端契约）：信号 → 登记全量基线请求
   （`_resync_pending`，下一周期 `_cycle_one` 头部消费）、本页游标不推进；
2) `_resync` 先墓碑后快照（8b 客户端契约）：tombstones 逐条 delete 应用完毕
   再并入 entities；单墓碑失败隔离（继续其余）；
3) 事件通道超时/尺寸闸（8d 客户端面）：`_client` 读超时抬高 + `_push` 按
   字节预算分块（`sync_client_batch_max_bytes`），单请求事件体积 ≤ 预算
   （单事件超预算则独占一块，不拆事件）。

RED 先行：以上行为在当前实现下必失败（信号不消费/墓碑不应用/无分块同源）。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_sync_client_w9_consumers.py -v
"""
import asyncio
import json
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
    def __init__(self):
        self.commits = 0
        self.nested = 0

    def begin_nested(self):
        db = self

        class _N:
            async def __aenter__(self):
                db.nested += 1
                return db

            async def __aexit__(self, *a):
                return False

        return _N()

    async def commit(self):
        self.commits += 1

    def add(self, obj):
        pass


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    cfg = SimpleNamespace(
        sync_client_batch=200,
        sync_client_batch_max_bytes=1024,
        sync_workspace_enabled=False,
        sync_memory_enabled=False,
    )
    monkeypatch.setattr(sc_mod, "get_config", lambda: cfg)
    return cfg


def _state():
    return SimpleNamespace(
        device_id="dev-1", pull_cursor=100, push_cursor=0,
        server_url="http://cloud", cloud_username="u", cloud_password="p",
        tls_verify=False, last_sync_at=None, last_error=None, user_id="u1",
    )


def _user():
    return SimpleNamespace(id="u1", username="u")


# ── 1) _pull 消费 resync_required ────────────────────────────────────────

def test_pull_signal_defers_cursor_and_schedules_resync(monkeypatch):
    c = SyncClient()
    state = _state()
    db = _FakeDB()
    page = {"events": [], "cursor": 180, "has_more": False,
            "min_available_seq": 500, "resync_required": True}
    calls = []

    async def fake_call(state_, client, method, path, **kw):
        calls.append((method, path))
        return _FakeResp(page)

    monkeypatch.setattr(c, "_call", fake_call)
    _run(c._pull(db, None, _user(), state))

    assert "u1" in c._resync_pending, "缺口信号必须登记全量基线请求（_resync_pending）"
    assert state.pull_cursor == 100, "信号页不得推进游标（防跨过缺口后信号永不可见）"


def test_pull_normal_advances_cursor(monkeypatch):
    c = SyncClient()
    state = _state()
    db = _FakeDB()
    page = {"events": [], "cursor": 160, "has_more": False}
    monkeypatch.setattr(c, "_call", lambda *a, **k: _run_ret(_FakeResp(page)))
    _run(c._pull(db, None, _user(), state))
    assert state.pull_cursor == 160
    assert "u1" not in c._resync_pending


async def _run_ret(v):
    return v


# ── 2) _resync 先墓碑后快照 + 失败隔离 ──────────────────────────────────

def test_resync_applies_tombstones_before_snapshots(monkeypatch):
    c = SyncClient()
    state = _state()
    db = _FakeDB()
    body = {
        "entities": {"messages": [{"id": "m-new"}]},
        "tombstones": [
            {"entity_type": "messages", "entity_id": "m-dead", "deleted_at": "2026-10-08T00:00:00Z"},
            {"entity_type": "notes", "entity_id": "n-dead", "deleted_at": "2026-10-08T00:00:00Z"},
        ],
        "cursor": 999,
    }
    order = []

    async def fake_apply(db_, user_, et, eid, op, payload):
        order.append((et, eid, op))
        return "applied"

    async def fake_call(state_, client, method, path, **kw):
        assert path == "/api/sync/resync"
        return _FakeResp(body)

    monkeypatch.setattr(sc_mod, "apply_change", fake_apply)
    monkeypatch.setattr(c, "_call", fake_call)
    monkeypatch.setattr(c, "_merge_default_assistant", lambda *a, **k: _run_ret(None))

    _run(c._resync(db, None, _user(), state))

    tbs = [o for o in order if o[2] == "delete"]
    ents = [o for o in order if o[2] != "delete"]
    assert len(tbs) == 2 and len(ents) == 1, f"墓碑与快照都应应用: {order}"
    assert order.index(tbs[-1]) < order.index(ents[0]), "先墓碑后快照（客户端契约）"
    assert ("messages", "m-dead", "delete") in order and ("notes", "n-dead", "delete") in order
    assert state.pull_cursor == 999


def test_resync_tombstone_failure_isolated(monkeypatch):
    c = SyncClient()
    state = _state()
    db = _FakeDB()
    body = {
        "entities": {"messages": [{"id": "m-new"}]},
        "tombstones": [
            {"entity_type": "messages", "entity_id": "m-bad"},
            {"entity_type": "messages", "entity_id": "m-good"},
        ],
        "cursor": 9,
    }
    done = []

    async def fake_apply(db_, user_, et, eid, op, payload):
        if eid == "m-bad":
            raise RuntimeError("poison tombstone")
        done.append((et, eid, op))
        return "applied"

    monkeypatch.setattr(sc_mod, "apply_change", fake_apply)
    monkeypatch.setattr(c, "_call", lambda *a, **k: _run_ret(_FakeResp(body)))
    monkeypatch.setattr(c, "_merge_default_assistant", lambda *a, **k: _run_ret(None))

    _run(c._resync(db, None, _user(), state))
    assert ("messages", "m-good", "delete") in done, "单墓碑毒事件不得顶死其余"
    assert ("messages", "m-new", "create") in done


def test_resync_tombstones_echo_suppressed(monkeypatch):
    """评审 M3：墓碑应用必须带 rx:resync 回显抑制（否则本地删除反推上游）。"""
    c = SyncClient()
    state = _state()
    db = _FakeDB()
    body = {"entities": {}, "tombstones": [
        {"entity_type": "messages", "entity_id": "m-dead"}]}
    origins = []
    monkeypatch.setattr(sc_mod, "set_origin_device", lambda v: origins.append(("set", v)) or "tok")
    monkeypatch.setattr(sc_mod, "reset_origin_device", lambda tok: origins.append(("reset", tok)))

    async def fake_apply(db_, user_, et, eid, op, payload):
        return "applied"

    monkeypatch.setattr(sc_mod, "apply_change", fake_apply)
    monkeypatch.setattr(c, "_call", lambda *a, **k: _run_ret(_FakeResp(body)))

    _run(c._resync(db, None, _user(), state))
    assert ("set", "rx:resync") in origins and any(o[0] == "reset" for o in origins),         f"墓碑应用必须设/复位 rx:resync：{origins}"


# ── 3) 事件通道超时/尺寸闸 ──────────────────────────────────────────────

def test_client_read_timeout_raised():
    c = SyncClient()
    client = c._client("http://cloud", tls_verify=False)
    try:
        assert client.timeout.read >= 60.0, "事件通道读超时必须抬高（大载荷/弱网）"
        assert client.timeout.write >= 30.0
    finally:
        _run(client.aclose())


def test_batch_max_bytes_property_clamps():
    from app.core.config import Config

    cfg = Config.__new__(Config)
    cfg._config = {"sync": {"client": {}}}
    assert cfg.sync_client_batch_max_bytes == 2 * 1024 * 1024, "默认 2 MiB"
    cfg._config = {"sync": {"client": {"batch_max_bytes": 10}}}
    assert cfg.sync_client_batch_max_bytes == 4096, "下限 4 KiB 钳制"
    cfg._config = {"sync": {"client": {"batch_max_bytes": "abc"}}}
    assert cfg.sync_client_batch_max_bytes == 2 * 1024 * 1024, "坏值回退默认"


def test_push_chunks_by_byte_budget(monkeypatch):
    c = SyncClient()
    state = _state()
    db = _FakeDB()

    rows = []
    for i in range(5):
        rows.append(SimpleNamespace(
            seq=i + 1, entity_type="messages", entity_id=f"m{i}", op="create",
            # CJK 载荷≈380 UTF-8 字节/条（字符数 ~100——旧字符口径在此盲：
            # 字符口径会把两条 760B 装进同一请求=实发 760B... 但预算断言按
            # 字节校验；更关键的是超长 CJK 的累积低估由 400 字符样例覆盖）
            payload={"id": f"m{i}", "blob": "汉" * 100},
        ))

    class _Rows:
        def __init__(self, rows_):
            self._rows = rows_

        def scalars(self):
            return self

        def all(self):
            return list(self._rows)

    calls = []

    class _DB(_FakeDB):
        def __init__(self):
            super().__init__()
            self._pending = [rows, []]

        async def execute(self, stmt):
            return _Rows(self._pending.pop(0) if self._pending else [])

    async def fake_call(state_, client, method, path, **kw):
        calls.append(kw.get("json"))
        return _FakeResp({"applied": [e["entity_id"] for e in kw["json"]["events"]], "skipped": []})

    db = _DB()
    monkeypatch.setattr(c, "_call", fake_call)
    _run(c._push(db, None, _user(), state))

    assert calls, "至少一次推送"
    assert db.commits == len(calls), "每块一提交（评审 M1：per-chunk commit 钉死）"
    for payload in calls:
        # 评审 I1：预算=**UTF-8 字节**（httpx 实发）——用 encode 后长度断言
        size = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
        assert size <= 1024, f"单请求 UTF-8 体积必须 ≤ 预算（实得 {size}）"
    sent = [e["entity_id"] for p in calls for e in p["events"]]
    assert sent == [f"m{i}" for i in range(5)], "事件顺序与完整性不得因分块改变"
    assert len(calls) >= 3, "5×~380B 在 1024B 预算下必须多块（约 2 条/块）"
    assert state.push_cursor == 5, "全部块成功后游标推进到最后一行"


def test_push_single_oversized_event_own_chunk(monkeypatch):
    """单事件超预算=独占一块且不拆（截断被禁）——该块允许超预算（既定语义）。"""
    c = SyncClient()
    state = _state()
    db = _FakeDB()

    class _Rows:
        def __init__(self, rows_):
            self._rows = rows_

        def scalars(self):
            return self

        def all(self):
            return list(self._rows)

    big = SimpleNamespace(seq=1, entity_type="messages", entity_id="m-big", op="create",
                          payload={"id": "m-big", "blob": "汉" * 1000})

    class _DB(_FakeDB):
        def __init__(self):
            super().__init__()
            self._pending = [[big], []]

        async def execute(self, stmt):
            return _Rows(self._pending.pop(0) if self._pending else [])

    calls = []

    async def fake_call(state_, client, method, path, **kw):
        calls.append(kw.get("json"))
        return _FakeResp({"applied": [], "skipped": []})

    monkeypatch.setattr(c, "_call", fake_call)
    _run(c._push(_DB(), None, _user(), state))
    assert len(calls) == 1 and len(calls[0]["events"]) == 1, "超件必须独占一块且不拆"
    assert state.push_cursor == 1


def test_push_chunk_failure_keeps_cursor_before_failed_chunk(monkeypatch):
    """评审 M2：分块失败=有界重试——游标停在失败块之前，异常上抛。"""
    c = SyncClient()
    state = _state()
    rows = [
        SimpleNamespace(seq=i + 1, entity_type="messages", entity_id=f"m{i}", op="create",
                        payload={"id": f"m{i}", "blob": "汉" * 400})
        for i in range(3)
    ]

    class _Rows:
        def __init__(self, rows_):
            self._rows = rows_

        def scalars(self):
            return self

        def all(self):
            return list(self._rows)

    class _DB(_FakeDB):
        def __init__(self):
            super().__init__()
            self._pending = [rows, []]

        async def execute(self, stmt):
            return _Rows(self._pending.pop(0) if self._pending else [])

    calls = []

    async def fake_call(state_, client, method, path, **kw):
        calls.append(kw.get("json"))
        if len(calls) == 2:
            raise RuntimeError("chunk-2 transient")
        return _FakeResp({"applied": [e["entity_id"] for e in kw["json"]["events"]], "skipped": []})

    db = _DB()
    monkeypatch.setattr(c, "_call", fake_call)
    with pytest.raises(RuntimeError):
        _run(c._push(db, None, _user(), state))
    assert len(calls) == 2
    assert state.push_cursor == 1, "失败块游标不得越过（下一周期自该块重试）"
    assert db.commits == 1, "仅成功块已提交（失败块不提交——评审 M1）"


def test_push_respects_server_event_count_cap(monkeypatch):
    """评审 M5：服务端 PushRequest.events max_length=500——分块须同限事件数。"""
    c = SyncClient()
    state = SimpleNamespace(device_id="dev-1", push_cursor=0)
    db = _FakeDB()
    cfg = sc_mod.get_config()
    cfg.sync_client_batch = 1000  # 显式放大计数批
    cfg.sync_client_batch_max_bytes = 10 * 1024 * 1024
    rows = [
        SimpleNamespace(seq=i + 1, entity_type="messages", entity_id=f"m{i}", op="create",
                        payload={"id": f"m{i}"})
        for i in range(600)
    ]

    class _Rows:
        def __init__(self, rows_):
            self._rows = rows_

        def scalars(self):
            return self

        def all(self):
            return list(self._rows)

    class _DB(_FakeDB):
        def __init__(self):
            super().__init__()
            self._pending = [rows, []]

        async def execute(self, stmt):
            return _Rows(self._pending.pop(0) if self._pending else [])

    calls = []

    async def fake_call(state_, client, method, path, **kw):
        calls.append(kw.get("json"))
        return _FakeResp({"applied": [], "skipped": []})

    monkeypatch.setattr(c, "_call", fake_call)
    _run(c._push(_DB(), None, _user(), state))
    assert sum(len(p["events"]) for p in calls) == 600
    for p in calls:
        assert len(p["events"]) <= 500, f"单请求事件数必须 ≤ 500（实得 {len(p['events'])}）"
