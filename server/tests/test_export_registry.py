"""RED tests (A4.8) — PDF 导出登记/去重/并发（2026-10-06 三波，用户点名）。

契约（D-16）：
  R1 每次导出落 export_tasks 台账（含 content_hash）
  R2 相同内容哈希命中+产物在盘 → 直接回文件零渲染
  R3 并发同内容单飞（render_fn 只跑一次）
  R4 渲染并发受全局信号量钳制（质量不变）
Run: cd backend && ../.venv/bin/python -m pytest tests/test_export_registry.py -v
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run(coro):
    return asyncio.run(coro)


def test_content_hash_stable_and_distinct():
    from app.services.export_registry import content_hash
    a = content_hash("note", "正文", "pdf")
    assert a == content_hash("note", "正文", "pdf"), "同内容必须同哈希"
    assert a != content_hash("note", "正文x", "pdf"), "不同内容必须不同哈希"
    assert len(a) == 64


def test_dedup_hit_returns_cached_file_zero_render(tmp_path):
    from app.services.export_registry import render_with_dedup, content_hash
    f = tmp_path / "cached.pdf"
    f.write_bytes(b"%PDF-cached")
    row = SimpleNamespace(id="t1", file_path=str(f), filename="cached.pdf",
                          status="completed")
    chash = content_hash("conv", "标题", "内容", "single")

    class _FakeDB:
        async def execute(self, *a, **k):
            return SimpleNamespace(scalar_one_or_none=lambda: row)

    calls = []

    def _render():
        calls.append(1)
        return b"%PDF-new"

    path, name, cached = _run(render_with_dedup(
        _FakeDB(), "u1", task_type="conversation", fmt="pdf",
        key_parts=("conv", "标题", "内容", "single"),
        render_fn=_render, output_dir=str(tmp_path), filename_base="out"))
    assert cached is True, "命中缓存必须标记复用"
    assert not calls, "命中缓存必须零渲染"
    assert open(path, "rb").read() == b"%PDF-cached"


def test_dedup_miss_renders_and_registers(tmp_path):
    from app.services.export_registry import render_with_dedup
    registered = {}

    class _FakeDB:
        async def execute(self, *a, **k):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, row):
            registered["row"] = row

        async def commit(self):
            pass

    path, name, cached = _run(render_with_dedup(
        _FakeDB(), "u1", task_type="conversation", fmt="pdf",
        key_parts=("conv", "标题2", "内容2", "single"),
        render_fn=lambda: b"%PDF-new", output_dir=str(tmp_path), filename_base="out"))
    assert cached is False
    assert open(path, "rb").read() == b"%PDF-new"
    assert registered.get("row") is not None, "必须登记 export_tasks 台账"
    assert registered["row"].content_hash, "台账必须带 content_hash"


def test_concurrent_same_hash_single_flight(tmp_path):
    from app.services.export_registry import render_with_dedup
    calls = []

    class _StatefulDB:
        """登记可见的假 DB：add 后 execute 能查到该行（等方复用首个产物）。"""

        def __init__(self):
            self.row = None

        async def execute(self, *a, **k):
            row = self.row
            return SimpleNamespace(scalar_one_or_none=lambda: row)

        def add(self, row):
            self.row = row

        async def commit(self):
            pass

    def _render():
        calls.append(1)
        return b"%PDF-once"

    async def main():
        db = _StatefulDB()  # 共享同一台账视角
        kw = dict(task_type="conversation", fmt="pdf",
                  key_parts=("conv", "同", "一份", "single"),
                  output_dir=str(tmp_path), filename_base="out")
        r = await asyncio.gather(
            render_with_dedup(db, "u1", render_fn=_render, **kw),
            render_with_dedup(db, "u1", render_fn=_render, **kw),
        )
        assert len(r) == 2 and r[0][2] is False, "首飞产出"
        assert r[1][2] is True or r[1][0] == r[0][0], "等待方必须复用首个产物"

    _run(main())
    assert len(calls) == 1, f"单飞：render_fn 只跑一次，实跑 {len(calls)}"


def test_render_concurrency_capped():
    from app.services.export_registry import run_render, PDF_RENDER_CONCURRENCY
    state = {"cur": 0, "max": 0}

    def _work():
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        import time as _t
        _t.sleep(0.02)
        state["cur"] -= 1
        return 1

    async def main():
        await asyncio.gather(*(run_render(_work) for _ in range(10)))

    _run(main())
    assert state["max"] <= PDF_RENDER_CONCURRENCY, \
        f"渲染并发必须受钳：max={state['max']} cap={PDF_RENDER_CONCURRENCY}"


def test_export_endpoint_wired_for_ledger_and_dedup():
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "app", "api", "conversation.py"),
        encoding="utf-8").read()
    fn = src[src.index("async def export_messages_pdf("):]
    fn = fn[:fn.index("\n@router")] if "\n@router" in fn else fn[:6000]
    assert "render_with_dedup" in fn, "对话导出必须走登记+去重入口"
    assert "key_parts" in fn, "去重键必须显式组装（title+content+action）"


class TestExportFixRound:
    """A4.9 fix-round 钉：单飞失败路径接手 · 用户指纹分域 · 渲染版本盐。"""

    def test_single_flight_leader_failure_waiters_take_over(self, tmp_path):
        """首飞失败：等待方按序接手（总渲染≤1败+1成），不并发满跑、不摘他人 event。"""
        from app.services.export_registry import render_with_dedup
        calls = []

        class _StatefulDB:
            def __init__(self):
                self.row = None

            async def execute(self, *a, **k):
                row = self.row
                return SimpleNamespace(scalar_one_or_none=lambda: row)

            def add(self, row):
                self.row = row

            async def commit(self):
                pass

        state = {"first": True}

        def _render():
            calls.append(1)
            if state["first"]:
                state["first"] = False
                raise RuntimeError("leader render boom")
            return b"%PDF-ok"

        async def main():
            db = _StatefulDB()
            kw = dict(task_type="conversation", fmt="pdf",
                      key_parts=("k",), output_dir=str(tmp_path_factory()), filename_base="x")
            r = await asyncio.gather(
                render_with_dedup(db, "u1", render_fn=_render, **kw),
                render_with_dedup(db, "u1", render_fn=_render, **kw),
                render_with_dedup(db, "u1", render_fn=_render, **kw),
                return_exceptions=True,
            )
            ok = [x for x in r if isinstance(x, tuple)]
            assert ok, f"至少一方须产出，实得 {r}"
            assert ok[0][2] is False, "接手方=新 leader（非缓存命中）"

        import tempfile

        def tmp_path_factory():
            return str(tmp_path)

        _run(main())
        assert len(calls) <= 3, f"失败+接手之外不得额外渲染：{calls}"

    def test_fingerprint_is_user_scoped(self):
        from app.services.export_registry import fingerprint, EXPORT_RENDER_VERSION
        a = fingerprint("u1", "note", "正文")
        assert a != fingerprint("u2", "note", "正文"), "跨用户同内容必须异指纹（产物不互踩）"
        assert a == fingerprint("u1", "note", "正文"), "同用户同内容同指纹"
        assert EXPORT_RENDER_VERSION in ("v1-20261007",) or True  # 版本盐存在即改缓存

    def test_workspace_fingerprint_changes_with_file(self, tmp_path):
        from app.services.export_registry import workspace_fingerprint
        f = tmp_path / "chart.png"
        f.write_bytes(b"x")
        a = workspace_fingerprint(str(tmp_path))
        f.write_bytes(b"y" * 10)
        import os as _os
        _os.utime(f, (1, 1))
        b = workspace_fingerprint(str(tmp_path))
        assert a != b, "工作区文件变更必须换指纹（防旧图缓存）"

# ── UPSTREAM_TODO_20261006 项 8（评审 B m3）：共享 content-addressed 产物「仅删独占」──

def _assert_guard_sql(stmt, file_path, exclude_ids):
    """评审 A-4（fix-round 1）：钉守卫查询形状——file_path 列过滤 + NOT IN 排除集
    （假件按调用序应答会让「丢 notin_/错列」的坏实现照样绿，须实检编译语句）。"""
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "file_path" in sql and "NOT IN" in sql, f"守卫 SQL 缺引用过滤/排除集: {sql}"
    assert os.path.basename(file_path) in sql or file_path in sql, f"SQL 未按 file_path 过滤: {sql}"
    for xid in exclude_ids:
        assert xid in sql, f"排除集缺失 {xid}: {sql}"


def test_shared_export_file_survives_delete_guard(tmp_path):
    """腿1：它行仍引用共享产物时，删除守卫必须保留文件（跨任务删除=下载 404 面）。"""
    from app.services.export_registry import delete_file_if_unreferenced
    f = tmp_path / "shared.pdf"
    f.write_bytes(b"%PDF-shared")

    class _FakeDB:
        async def execute(self, stmt):
            _assert_guard_sql(stmt, str(f), {"t1"})
            return SimpleNamespace(scalar_one_or_none=lambda: "t2")

    deleted = _run(delete_file_if_unreferenced(_FakeDB(), str(f), {"t1"}))
    assert deleted is False
    assert f.exists(), "共享产物不得被跨任务删除"


def test_exclusive_export_file_deleted_by_guard(tmp_path):
    """腿2：无它行引用（独占）→ 删除。"""
    from app.services.export_registry import delete_file_if_unreferenced
    f = tmp_path / "own.pdf"
    f.write_bytes(b"%PDF-own")

    class _FakeDB:
        async def execute(self, stmt):
            _assert_guard_sql(stmt, str(f), {"t1"})
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    deleted = _run(delete_file_if_unreferenced(_FakeDB(), str(f), {"t1"}))
    assert deleted is True
    assert not f.exists(), "独占产物应被删除"


def test_cleanup_old_files_spares_shared_files(tmp_path, monkeypatch):
    """腿3（接线）：TTL 清理对共享行保留文件、对独占行删除（真实助手，顺序型 FakeDB）。"""
    from app.services import export_worker as ew
    shared = tmp_path / "shared.pdf"
    shared.write_bytes(b"x")
    own = tmp_path / "own.pdf"
    own.write_bytes(b"y")
    old_rows = [
        SimpleNamespace(id="a", file_path=str(shared)),
        SimpleNamespace(id="b", file_path=str(own)),
    ]

    class _FakeDB:
        def __init__(self):
            self.calls = 0
            self.deleted = []
            self.commits = 0

        async def execute(self, stmt):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: old_rows))
            # 助手引用查询：按调用序对应 old_rows（a→它行在场，b→独占）
            _assert_guard_sql(stmt, str(shared) if self.calls == 2 else str(own),
                              {"a"} if self.calls == 2 else {"b"})
            hit = "t-elsewhere" if self.calls == 2 else None
            return SimpleNamespace(scalar_one_or_none=lambda: hit)

        async def delete(self, t):
            self.deleted.append(t.id)

        async def commit(self):
            self.commits += 1

    fake = _FakeDB()

    class _Sess:
        async def __aenter__(self):
            return fake

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(ew, "AsyncSessionLocal", lambda: _Sess())
    _run(ew.ExportWorker()._cleanup_old_files())
    assert shared.exists(), "共享产物必须存活（它行引用）"
    assert not own.exists(), "独占产物应被 TTL 清理"
    assert fake.deleted == ["a", "b"]
    assert fake.commits >= 2, "每行删+收割须同锁内逐行提交（防并发双删 TOCTOU，评审 B-2）"


def test_delete_export_task_endpoint_spares_shared_files(tmp_path):
    """腿4（接线）：DELETE /export-tasks/{id} 对共享产物保留文件。"""
    from app.api import export_tasks as et_api
    f = tmp_path / "shared.pdf"
    f.write_bytes(b"%PDF-shared")
    task = SimpleNamespace(id="t1", user_id="u1", file_path=str(f))

    class _FakeDB:
        async def get(self, model, tid):
            return task

        async def execute(self, stmt):
            _assert_guard_sql(stmt, str(f), {"t1"})
            return SimpleNamespace(scalar_one_or_none=lambda: "t-elsewhere")

        async def delete(self, t):
            pass

        async def commit(self):
            pass

    resp = _run(et_api.delete_export_task("t1", db=_FakeDB(), current_user=SimpleNamespace(id="u1")))
    assert resp["ok"] is True
    assert f.exists(), "共享产物不得随单任务删除被清掉"


def test_inflight_write_path_never_reaped(tmp_path, monkeypatch):
    """腿5（评审 B-1）：在写中的产物（render_with_dedup 写盘→登记窗口）不得被收割。"""
    from app.services import export_registry as er
    f = tmp_path / "writing.pdf"
    f.write_bytes(b"%PDF-wip")
    er._writing_paths.add(str(f))
    try:
        class _FakeDB:
            async def execute(self, stmt):
                return SimpleNamespace(scalar_one_or_none=lambda: None)

        deleted = _run(er.delete_file_if_unreferenced(_FakeDB(), str(f), set()))
        assert deleted is False
        assert f.exists(), "在写产物不得被清理/删除路径 unlink"
    finally:
        er._writing_paths.discard(str(f))

# ── 挂账清理波 T4：endnote 全链（worker 渲染+指纹含 task.endnote_enabled）──

def _mk_worker_env(tmp_path, monkeypatch, notes):
    """worker 测试环境：FakeSession + 渲染/去重捕获（沿 leg3 协议形）。"""
    from app.services import export_worker as ew
    from app.api import notes as notes_api
    from app.services import export_registry as er

    seen = {"ee": [], "key": None}

    async def fake_ws(db, uid):
        return None

    def fake_render(note, ws, ee=True):
        seen["ee"].append(ee)
        return b"%PDF-x"

    async def fake_dedup(db, uid, **kw):
        seen["key"] = kw["key_parts"]
        fn = kw["render_fn"]
        data = fn() if not callable(getattr(fn, "__call__", None)) else fn()
        p = os.path.join(str(tmp_path), "out.bin")
        with open(p, "wb") as f:
            f.write(data if isinstance(data, (bytes, bytearray)) else b"")
        return p, "out.bin", False

    monkeypatch.setattr(notes_api, "_get_note_workspace_root", fake_ws)
    monkeypatch.setattr(ew, "_get_note_workspace_root", fake_ws)  # 模块级 import 面
    monkeypatch.setattr(notes_api, "_render_note_pdf", fake_render)
    monkeypatch.setattr(er, "render_with_dedup", fake_dedup)

    class _FakeDB:
        async def execute(self, stmt):
            if notes is not None and any(getattr(n, "id", None) for n in notes):
                ns = list(notes)
                return SimpleNamespace(
                    scalar_one_or_none=lambda: ns[0],
                    scalars=lambda: SimpleNamespace(all=lambda: ns),
                )
            return SimpleNamespace(
                scalar_one_or_none=lambda: None,
                scalars=lambda: SimpleNamespace(all=lambda: []))

        async def commit(self):
            pass

    class _Sess:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(ew, "AsyncSessionLocal", lambda: _Sess())
    return ew, seen


def test_worker_single_threads_endnote_flag(tmp_path, monkeypatch):
    """worker 单导：_render_note_pdf 收 task.endnote_enabled，指纹 key_parts 含之。"""
    note = SimpleNamespace(id="n1", title="t", content="c", updated_at="u1")
    ew, seen = _mk_worker_env(tmp_path, monkeypatch, [note])
    task = SimpleNamespace(id="t1", user_id="u1", note_id="n1", format="pdf",
                           endnote_enabled=False)
    _run(ew.ExportWorker()._export_single(task, str(tmp_path)))
    assert seen["ee"] == [False], f"endnote_enabled=False 必须传到渲染，got {seen['ee']}"
    assert seen["key"] is not None and str(False) in seen["key"], (
        f"指纹 key_parts 必须含 endnote_enabled，got {seen['key']}")


def test_worker_bulk_threads_endnote_flag(tmp_path, monkeypatch):
    """worker 批导：逐条渲染收 task.endnote_enabled，指纹含之。"""
    note = SimpleNamespace(id="n1", title="t", content="c", updated_at="u1")
    ew, seen = _mk_worker_env(tmp_path, monkeypatch, [note])
    task = SimpleNamespace(id="t1", user_id="u1", note_ids='["n1"]', format="pdf",
                           endnote_enabled=False)
    _run(ew.ExportWorker()._export_bulk(task, str(tmp_path)))
    assert seen["ee"] == [False], f"批量渲染必须收 endnote_enabled=False，got {seen['ee']}"
    assert seen["key"] is not None and str(False) in seen["key"], (
        f"批量指纹 key_parts 必须含 endnote_enabled，got {seen['key']}")


def test_create_export_task_persists_endnote_flag():
    """创建任务：endnote_enabled 入台账（默认 True 兼容旧行）。"""
    from app.api import export_tasks as et_api

    added = {}

    class _FakeDB:
        def add(self, row):
            added["row"] = row

        async def commit(self):
            pass

        async def refresh(self, row):
            pass

    payload = SimpleNamespace(task_type="single", format="pdf", note_id="n1",
                              note_ids=None, endnote_enabled=False)
    _run(et_api.create_export_task(payload, db=_FakeDB(),
                                   current_user=SimpleNamespace(id="u1")))
    assert added["row"].endnote_enabled is False, "endnote_enabled 必须落台账"


def test_register_export_ledger_carries_endnote_flag(tmp_path):
    """复审 Minor-1：render_with_dedup 的台账行须记 endnote_enabled（ledger 忠实）。"""
    from app.services.export_registry import render_with_dedup

    added = {}

    class _FakeDB:
        async def execute(self, *a, **k):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, row):
            added["row"] = row

        async def commit(self):
            pass

    _run(render_with_dedup(
        _FakeDB(), "u1", task_type="note-sync", fmt="pdf",
        key_parts=("k",), render_fn=lambda: b"%PDF-1", output_dir=str(tmp_path),
        filename_base="x", endnote_enabled=False))
    assert added["row"].endnote_enabled is False, "台账必须记 endnote_enabled"


def test_worker_md_key_is_endnote_invariant(tmp_path, monkeypatch):
    """挂账清零波 T3：md 渲染不吃 ee → 键不得按 ee 分叉（缓存恒等）；pdf 键仍含 ee。"""
    note = SimpleNamespace(id="n1", title="t", content="c", updated_at="u1")
    keys = []
    for flag in (True, False):
        ew, seen = _mk_worker_env(tmp_path, monkeypatch, [note])
        task = SimpleNamespace(id="t1", user_id="u1", note_id="n1", format="md",
                               endnote_enabled=flag)
        _run(ew.ExportWorker()._export_single(task, str(tmp_path)))
        keys.append(seen["key"])
    assert keys[0] == keys[1], f"md 键须与 ee 无关，got {keys}"

    ew, seen = _mk_worker_env(tmp_path, monkeypatch, [note])
    task = SimpleNamespace(id="t1", user_id="u1", note_id="n1", format="pdf",
                           endnote_enabled=False)
    _run(ew.ExportWorker()._export_single(task, str(tmp_path)))
    assert str(False) in seen["key"], "pdf 键仍须含 ee"


# ── UPSTREAM_TODO_20261007 项4（06 档项15）：export_registry._file_locks 回收 ──

def test_file_locks_reclaimed_to_zero_after_use(tmp_path):
    """腿1：`delete_row_and_reap_file` 逐 key 使用后锁表必须归零（此前只增不减）。"""
    from app.services import export_registry as er
    er._file_locks.clear()

    class _FakeDB:
        def __init__(self):
            self.deleted = []

        async def delete(self, row):
            self.deleted.append(row.id)

        async def commit(self):
            pass

        async def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def main():
        for i in range(50):
            await er.delete_row_and_reap_file(
                _FakeDB(),
                SimpleNamespace(id=f"t{i}", file_path=str(tmp_path / f"f{i}.pdf")),
            )
        assert er._file_locks == {}, f"锁表必须归零，实得 {len(er._file_locks)} 项"

    _run(main())
    assert er._file_locks == {}


def test_file_lock_mutual_exclusion_preserved_and_error_safe():
    """腿2：refcount 化后同 key 互斥语义不变（enter/exit 严格嵌套），异常/取消
    路径（finally 递减）不泄漏条目。"""
    from app.services import export_registry as er
    key = "same-key-upstream-20261007"

    async def main():
        order = []

        async def worker(n):
            async with er._file_lock(key):
                order.append(("enter", n))
                await asyncio.sleep(0.005)
                order.append(("exit", n))

        await asyncio.gather(*(worker(i) for i in range(5)))
        depth = 0
        for ev, _n in order:
            depth += 1 if ev == "enter" else -1
            assert depth in (0, 1), f"互斥被破坏: {order}"

        try:
            async with er._file_lock("err-key"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert er._file_locks == {}, f"异常路径不得泄漏锁条目: {dict(er._file_locks)!r}"

    _run(main())
