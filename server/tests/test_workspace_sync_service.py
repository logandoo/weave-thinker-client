#!/usr/bin/env python3
"""RED tests (A4.8) — 云端 workspace 物化原语（2026-09-29 波；standalone 运行）。

契约（tests/acceptance.md 判据 1/2/4/5/6）：
  - safe_target：只接受相对 rel_path；`..`/绝对路径/空/NUL 一律 ValueError；
    已存在的符号链接父目录逃逸 workspace 时拒绝；允许指向尚不存在的文件
    （下载落点，父目录后建）。
  - should_pull：本地缺失 → 拉；远端 mtime 显著更新（> 容差）→ 拉；
    远端旧/持平 → 不拉（本地优先，幂等）。
  - walk_remote：递归远端清单（/api/files/list 形态，None=目录 404）；
    跳过内部目录与点文件；子目录缺失不连坐；深度有界。
  - remote_workspace_stat：根级文件父目录取 "."；命中返回 size/mtime；
    缺失/父目录缺失 → exists False。

Run: .localstack/venv/bin/python server/tests/test_workspace_sync_service.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.workspace_sync_service import (  # noqa: E402
    MAX_WALK_DEPTH,
    local_file_state,
    normalize_workspace_path,
    remote_workspace_stat,
    safe_target,
    sanitize_remote_mtime,
    should_pull,
    sweep_stale_syncparts,
    walk_remote,
)
from app.services.workspace_files_service import list_directory  # noqa: E402

RESULTS = []


def check(desc, ok, detail=""):
    RESULTS.append((desc, bool(ok), detail))


_TMP = Path(tempfile.mkdtemp(prefix="ws-sync-test-"))


def _fixture():
    root = _TMP / "weave" / "user-abc12345"
    (root / "uploads").mkdir(parents=True, exist_ok=True)
    (root / "noteimg").mkdir(exist_ok=True)
    (root / "uploads" / "a.bin").write_bytes(b"a" * 10)
    return root


def _expect_value_error(fn, *args):
    try:
        fn(*args)
    except ValueError:
        return True
    return False


def _run_checks():
    # ── safe_target ──────────────────────────────────────────────────────────
    ws = _fixture()

    got = safe_target(ws, "uploads/new.bin")
    check("safe_target 接受缺失相对路径（下载落点）",
          got == (ws / "uploads" / "new.bin").resolve() and not got.exists(), str(got))
    check("safe_target 接受根级文件", safe_target(ws, "README.md") == (ws / "README.md").resolve())

    for bad in ("../escape.bin", "uploads/../../escape.bin", "/tmp/escape.bin",
                "\\\\server\\share\\x", "", "  ", "a\x00b", "a\nb", "a\rb"):
        check(f"safe_target 拒绝越权/非法: {bad!r}", _expect_value_error(safe_target, ws, bad))

    outside = _TMP / "outside"
    outside.mkdir(exist_ok=True)
    (ws / "link_out").symlink_to(outside, target_is_directory=True)
    check("safe_target 拒绝符号链接父目录逃逸", _expect_value_error(safe_target, ws, "link_out/pwn.bin"))

    # ── local_file_state ─────────────────────────────────────────────────────
    st = local_file_state(ws, "uploads/a.bin")
    check("local_file_state 既有文件 size", st["exists"] is True and st["size"] == 10, str(st))

    st2 = local_file_state(ws, "uploads/nope.bin")
    check("local_file_state 缺失文件", st2["exists"] is False and st2["size"] is None, str(st2))
    check("local_file_state 目录不算文件", local_file_state(ws, "uploads")["exists"] is False)
    check("local_file_state 非法路径按缺席（不抛）", local_file_state(ws, "../evil")["exists"] is False)

    # ── should_pull ──────────────────────────────────────────────────────────
    check("should_pull 本地缺失 → True",
          should_pull({"size": 5, "mtime": 100.0}, {"exists": False, "size": None, "mtime": None}) is True)
    check("should_pull 远端显著更新 → True",
          should_pull({"size": 5, "mtime": 105.0}, {"exists": True, "size": 5, "mtime": 100.0}) is True)
    check("should_pull 远端旧 → False（本地优先）",
          should_pull({"size": 5, "mtime": 99.0}, {"exists": True, "size": 5, "mtime": 100.0}) is False)
    check("should_pull 持平 → False（幂等，不重下）",
          should_pull({"size": 5, "mtime": 100.0}, {"exists": True, "size": 5, "mtime": 100.0}) is False)
    # 评审 Important #1：对齐语义下无容差死区——远端 +0.1s 也必须拉取
    check("should_pull 远端 +0.1s → True（严格比较，无死区）",
          should_pull({"size": 5, "mtime": 100.1}, {"exists": True, "size": 5, "mtime": 100.0}) is True)
    check("should_pull 同 mtime 但 size 变 → True（同秒替换）",
          should_pull({"size": 6, "mtime": 100.0}, {"exists": True, "size": 5, "mtime": 100.0}) is True)
    check("should_pull 无 mtime 回退 size 比较",
          should_pull({"size": 6, "mtime": None}, {"exists": True, "size": 5, "mtime": None}) is True
          and should_pull({"size": 5, "mtime": None}, {"exists": True, "size": 5, "mtime": None}) is False)

    # ── sanitize_remote_mtime（评审 Important #3）────────────────────────────
    check("mtime 消毒：正常值原样",
          sanitize_remote_mtime(1700000000.5) == 1700000000.5)
    check("mtime 消毒：inf/NaN/负数/非数 → None",
          sanitize_remote_mtime(float("inf")) is None
          and sanitize_remote_mtime(float("nan")) is None
          and sanitize_remote_mtime(-1) is None
          and sanitize_remote_mtime("abc") is None
          and sanitize_remote_mtime(None) is None)
    _far_future = time.time() + 86400 * 30
    check("mtime 消毒：远未来（>now+24h）→ None",
          sanitize_remote_mtime(_far_future) is None)


    # ── walk_remote ──────────────────────────────────────────────────────────
    def _tree_listings():
        return {
            ".": {"path": "", "entries": [
                {"name": "uploads", "rel_path": "uploads", "is_dir": True, "size": 0, "mtime": 0, "type": "folder"},
                {"name": "noteimg", "rel_path": "noteimg", "is_dir": True, "size": 0, "mtime": 0, "type": "folder"},
                {"name": ".git", "rel_path": ".git", "is_dir": True, "size": 0, "mtime": 0, "type": "folder"},
                {"name": "node_modules", "rel_path": "node_modules", "is_dir": True, "size": 0, "mtime": 0, "type": "folder"},
                {"name": "README.md", "rel_path": "README.md", "is_dir": False, "size": 30, "mtime": 100.0, "type": "markdown"},
                {"name": ".DS_Store", "rel_path": ".DS_Store", "is_dir": False, "size": 6, "mtime": 10.0, "type": "file"},
            ]},
            "uploads": {"path": "uploads", "entries": [
                {"name": "a.bin", "rel_path": "uploads/a.bin", "is_dir": False, "size": 10, "mtime": 200.0, "type": "file"},
                {"name": "sub", "rel_path": "uploads/sub", "is_dir": True, "size": 0, "mtime": 0, "type": "folder"},
            ]},
            "uploads/sub": {"path": "uploads/sub", "entries": [
                {"name": "c.txt", "rel_path": "uploads/sub/c.txt", "is_dir": False, "size": 7, "mtime": 300.0, "type": "text"},
            ]},
            "noteimg": {"path": "noteimg", "entries": [
                {"name": "img.png", "rel_path": "noteimg/img.png", "is_dir": False, "size": 99, "mtime": 150.0, "type": "image"},
            ]},
        }


    listings = _tree_listings()
    calls = []


    async def fake_list(rel, offset=0):
        calls.append(rel)
        return listings[rel]


    manifest = asyncio.run(walk_remote(fake_list))
    by_rel = {e["rel_path"]: e for e in manifest}
    check("walk_remote 递归全集 + 跳过内部目录/点文件",
          set(by_rel) == {"README.md", "uploads/a.bin", "uploads/sub/c.txt", "noteimg/img.png"},
          str(sorted(by_rel)))
    check("walk_remote 条目字段（size/mtime）",
          by_rel.get("uploads/a.bin", {}).get("size") == 10
          and by_rel.get("uploads/sub/c.txt", {}).get("mtime") == 300.0)
    check("walk_remote 不遍历 .git/node_modules",
          ".git" not in calls and "node_modules" not in calls, str(calls))

    listings2 = _tree_listings()
    del listings2["noteimg"]


    async def fake_list_missing(rel, offset=0):
        return listings2.get(rel)  # None = 目录 404


    manifest2 = asyncio.run(walk_remote(fake_list_missing))
    check("walk_remote 子目录缺失不连坐",
          "uploads/a.bin" in {e["rel_path"] for e in manifest2})

    _depth = MAX_WALK_DEPTH + 3


    async def fake_list_deep(rel, offset=0):
        d = 0 if rel == "." else rel.count("/") + 1
        entries = []
        if d < _depth:
            entries.append({"name": "d", "rel_path": ("d" if rel == "." else f"{rel}/d"),
                            "is_dir": True, "size": 0, "mtime": 0, "type": "folder"})
        entries.append({"name": "f.txt", "rel_path": ("f.txt" if rel == "." else f"{rel}/f.txt"),
                        "is_dir": False, "size": 1, "mtime": 1.0, "type": "text"})
        return {"path": rel, "entries": entries}


    manifest3 = asyncio.run(walk_remote(fake_list_deep))
    check("walk_remote 深度有界",
          max(e["rel_path"].count("/") for e in manifest3) <= MAX_WALK_DEPTH,
          "deepest=%d" % max(e["rel_path"].count("/") for e in manifest3))

    # 截断通知（评审 Important #4）
    _trunc_calls = []


    async def fake_list_trunc(rel, offset=0):
        return {"path": rel, "truncated": True, "entries": [
            {"name": "only.bin", "rel_path": "only.bin", "is_dir": False,
             "size": 1, "mtime": 1.0, "type": "file"},
        ]}


    manifest_t = asyncio.run(walk_remote(fake_list_trunc, on_truncated=_trunc_calls.append))
    check("walk_remote 截断目录经 on_truncated 通知",
          _trunc_calls == ["."] and {e["rel_path"] for e in manifest_t} == {"only.bin"},
          f"{_trunc_calls} {manifest_t}")

    # ── remote_workspace_stat ────────────────────────────────────────────────
    seen = []


    async def stat_list(rel, offset=0):
        seen.append(rel)
        return {"path": "", "entries": [
            {"name": "README.md", "rel_path": "README.md", "is_dir": False, "size": 30, "mtime": 100.0, "type": "markdown"},
        ]}


    got = asyncio.run(remote_workspace_stat(stat_list, "README.md"))
    check("remote_stat 根级文件父目录='.' 且命中",
          seen == ["."] and got == {"exists": True, "size": 30, "mtime": 100.0}, f"{seen} {got}")
    check("remote_stat 缺失 → exists False",
          asyncio.run(remote_workspace_stat(stat_list, "nope.bin"))["exists"] is False)

    calls2 = []


    async def stat_list2(rel, offset=0):
        calls2.append(rel)
        if rel == "uploads/sub":
            return None
        return {"path": rel, "entries": [
            {"name": "a.bin", "rel_path": "uploads/a.bin", "is_dir": False, "size": 10, "mtime": 200.0, "type": "file"},
        ]}


    got2 = asyncio.run(remote_workspace_stat(stat_list2, "uploads/a.bin"))
    check("remote_stat 嵌套路径父目录正确",
          calls2 == ["uploads"] and got2["exists"] is True and got2["size"] == 10, f"{calls2} {got2}")
    check("remote_stat 父目录缺失 → exists False",
          asyncio.run(remote_workspace_stat(stat_list2, "uploads/sub/missing.bin"))["exists"] is False)


    async def stat_list_trunc(rel, offset=0):
        return {"path": rel, "truncated": True, "entries": [
            {"name": "a.bin", "rel_path": "a.bin", "is_dir": False, "size": 10, "mtime": 200.0, "type": "file"},
        ]}


    check("remote_stat 截断未命中 → exists None（未知，非不存在）",
          asyncio.run(remote_workspace_stat(stat_list_trunc, "b.bin"))["exists"] is None)
    check("remote_stat 截断但命中 → exists True",
          asyncio.run(remote_workspace_stat(stat_list_trunc, "a.bin"))["exists"] is True)

    # ── 分页（2026-09-29 续波，判据 1/2/3/4）───────────────────────────────
    _PAGE = 1000


    def _paged_bulk(total: int, offset: int, page: int = _PAGE):
        names = [f"f{i:04d}.bin" for i in range(total)]
        chunk = names[offset:offset + page]
        entries = [{"name": n, "rel_path": f"bulk/{n}", "is_dir": False,
                    "size": 1, "mtime": 1.0, "type": "file"} for n in chunk]
        return {"path": "bulk", "entries": entries,
                "truncated": offset + len(chunk) < total,
                "total": total, "offset": offset, "limit": page}


    _paged_calls = []


    async def paged_root(rel, offset=0):
        _paged_calls.append((rel, offset))
        if rel == ".":
            return {"path": "", "entries": [
                {"name": "bulk", "rel_path": "bulk", "is_dir": True, "size": 0, "mtime": 0, "type": "folder"},
            ]}
        if rel == "bulk":
            return _paged_bulk(1005, offset)
        return None


    manifest_paged = asyncio.run(walk_remote(paged_root))
    check("分页：1005 条跨 2 页进入清单（含第 1001+）",
          len(manifest_paged) == 1005
          and manifest_paged[-1]["rel_path"] == "bulk/f1004.bin"
          and ("bulk", 0) in _paged_calls and ("bulk", 1000) in _paged_calls,
          f"n={len(manifest_paged)} calls={_paged_calls}")


    async def paged_stat(rel, offset=0):
        return _paged_bulk(1005, offset)


    got_paged = asyncio.run(remote_workspace_stat(paged_stat, "bulk/f1004.bin"))
    check("分页 stat：第 1001+ 文件命中（exists True）",
          got_paged["exists"] is True and got_paged["size"] == 1, str(got_paged))
    check("分页 stat：不存在于末页 → exists False",
          asyncio.run(remote_workspace_stat(paged_stat, "bulk/nope.bin"))["exists"] is False)


    async def legacy_trunc(rel, offset=0):
        # 旧服务端：无 offset/total 字段，1000 截断
        return {"path": rel, "truncated": True, "entries": [
            {"name": f"f{i:04d}.bin", "rel_path": f"bulk/f{i:04d}.bin", "is_dir": False,
             "size": 1, "mtime": 1.0, "type": "file"} for i in range(1000)
        ]}


    _legacy_trunc_calls = []
    manifest_legacy = asyncio.run(walk_remote(legacy_trunc, on_truncated=_legacy_trunc_calls.append))
    check("旧服务端兼容：不翻页（1 次调用）+ truncated 告警保留 + 1000 条",
          len(_legacy_trunc_calls) == 1 and len(manifest_legacy) == 1000,
          f"calls={len(_legacy_trunc_calls)} n={len(manifest_legacy)}")
    check("旧服务端兼容：stat 截断未命中 → exists None",
          asyncio.run(remote_workspace_stat(legacy_trunc, "bulk/nope.bin"))["exists"] is None)


    async def no_progress(rel, offset=0):
        # 恶意/坏服务端：声称有 offset 但永远返回同一页（无进展）→ 必须终止不死循环
        return {"path": rel, "entries": [
            {"name": "a.bin", "rel_path": "a.bin", "is_dir": False, "size": 1, "mtime": 1.0, "type": "file"},
        ], "truncated": True, "total": 10**9, "offset": 0, "limit": _PAGE}


    _no_progress_calls = []
    manifest_np = asyncio.run(walk_remote(no_progress, on_truncated=lambda r: _no_progress_calls.append(r)))
    check("无进展防护：页数上限内终止 + 告警 + 清单有界",
          _no_progress_calls == ["."] and 1 <= len(manifest_np) <= 32,
          f"calls_trunc={_no_progress_calls} n={len(manifest_np)}")

    # 评审 R2 Minor 回归：整页仅子目录（合法）不得被误判无进展而漏拉后续文件
    _dirpage_calls = []


    async def dirs_then_file(rel, offset=0):
        _dirpage_calls.append((rel, offset))
        if rel != ".":
            return {"path": rel, "entries": [], "truncated": False, "total": 0,
                    "offset": offset, "limit": 1000}
        if offset == 0:
            return {"path": "", "entries": [
                {"name": f"d{i:04d}", "rel_path": f"d{i:04d}", "is_dir": True,
                 "size": 0, "mtime": 0, "type": "folder"} for i in range(1000)
            ], "truncated": True, "total": 1001, "offset": 0, "limit": 1000}
        return {"path": "", "entries": [
            {"name": "tail.bin", "rel_path": "tail.bin", "is_dir": False,
             "size": 3, "mtime": 1.0, "type": "file"},
        ], "truncated": False, "total": 1001, "offset": 1000, "limit": 1000}


    _m_dirs = asyncio.run(walk_remote(dirs_then_file))
    check("分页：整页子目录（合法）不误判无进展——第 2 页文件仍可达",
          "tail.bin" in {e["rel_path"] for e in _m_dirs}
          and (".", 1000) in _dirpage_calls,
          f"calls={_dirpage_calls[:3]} n={len(_m_dirs)}")

    # ── list_directory 分页契约（判据 1）────────────────────────────────────
    _ld_root = _TMP / "paged_ws"
    (_ld_root / "bulk").mkdir(parents=True, exist_ok=True)
    for i in range(105):
        (_ld_root / "bulk" / f"f{i:04d}.bin").write_bytes(b"x")

    page1 = list_directory(_ld_root, "bulk", offset=0, limit=100)
    check("list_directory 分页：首页 100 条 + truncated + total=105",
          len(page1["entries"]) == 100 and page1["truncated"] is True
          and page1["total"] == 105 and page1["offset"] == 0 and page1["limit"] == 100,
          str({k: page1[k] for k in ("truncated", "total", "offset", "limit")}))
    page2 = list_directory(_ld_root, "bulk", offset=100, limit=100)
    check("list_directory 分页：末页 5 条 + truncated False + offset 回显",
          len(page2["entries"]) == 5 and page2["truncated"] is False and page2["offset"] == 100)
    check("list_directory 分页：offset 越界 → 空页",
          list_directory(_ld_root, "bulk", offset=10**6, limit=100)["entries"] == [])
    _ld_def = list_directory(_ld_root, "bulk")
    check("list_directory 默认参数：<1000 目录全量返回且不截断",
          len(_ld_def["entries"]) == 105 and _ld_def["truncated"] is False
          and _ld_def["total"] == 105)

    # 默认参数真实边界：1005 条 → 首页 1000 + truncated（旧语义回归）
    _ld_big = _TMP / "default_1005"
    (_ld_big / "bulk").mkdir(parents=True, exist_ok=True)
    for i in range(1005):
        (_ld_big / "bulk" / f"g{i:04d}.bin").write_bytes(b"x")
    _ld_big_page = list_directory(_ld_big, "bulk")
    check("list_directory 默认参数边界：1005 条 → 1000 + truncated=True + total=1005",
          len(_ld_big_page["entries"]) == 1000 and _ld_big_page["truncated"] is True
          and _ld_big_page["total"] == 1005,
          f"n={len(_ld_big_page['entries'])} t={_ld_big_page['truncated']}")

    # 评审 Critical 回归：过滤条目（symlink）不影响分页偏移/truncated 语义
    _ld2 = _TMP / "paged_symlinks"
    (_ld2 / "bulk").mkdir(parents=True, exist_ok=True)
    for i in range(1005):
        (_ld2 / "bulk" / f"f{i:04d}.bin").write_bytes(b"x")
    _ld2_out = _TMP / "outside_target"
    _ld2_out.mkdir(exist_ok=True)
    for i in range(5):
        (_ld2 / "bulk" / f"s{i:04d}.link").symlink_to(_ld2_out / f"t{i}.bin")
    _p1 = list_directory(_ld2, "bulk", offset=0, limit=1000)
    _p2 = list_directory(_ld2, "bulk", offset=1000, limit=1000)
    check("分页+过滤：total=可见 1005（不含 symlink）· 首页 1000 · 末页 5 · truncated 正确",
          _p1["total"] == 1005 and len(_p1["entries"]) == 1000 and _p1["truncated"] is True
          and len(_p2["entries"]) == 5 and _p2["truncated"] is False,
          f"total={_p1['total']} p1={len(_p1['entries'])} p2={len(_p2['entries'])}")

    # 评审 Critical 回归（客户端）：服务端先切页后过滤时按服务端页长推进（不得重叠重取）
    _raw_calls = []


    async def raw_filtered(rel, offset=0):
        _raw_calls.append((rel, offset))
        # raw 页 0 含 995 个文件 + 5 个被过滤项（total=raw 1005）；按 limit 推进
        entries = [{"name": f"r{i:04d}.bin", "rel_path": f"raw/r{i:04d}.bin", "is_dir": False,
                    "size": 1, "mtime": 1.0, "type": "file"} for i in range(995)]
        if offset == 0:
            return {"path": "raw", "entries": entries, "truncated": True,
                    "total": 1005, "offset": 0, "limit": 1000}
        return {"path": "raw", "entries": [
            {"name": f"r{i:04d}.bin", "rel_path": f"raw/r{i:04d}.bin", "is_dir": False,
             "size": 1, "mtime": 1.0, "type": "file"} for i in range(995, 1005)
        ], "truncated": False, "total": 1005, "offset": 1000, "limit": 1000}


    _m_raw = asyncio.run(walk_remote(raw_filtered))
    check("分页+过滤：客户端按服务端页长推进（无重叠重取，尾部可达）",
          (".", 0) in _raw_calls and (".", 1000) in _raw_calls
          and len(_m_raw) == 1005 and len({e["rel_path"] for e in _m_raw}) == 1005,
          f"calls={_raw_calls} n={len(_m_raw)}")

    # ── .syncpart 清扫（判据 5）────────────────────────────────────────────
    _sw_root = _TMP / "sweep_ws"
    (_sw_root / "uploads" / "sub").mkdir(parents=True, exist_ok=True)
    _stale = _sw_root / "uploads" / ".a.bin.deadbeef.syncpart"
    _fresh = _sw_root / "uploads" / ".b.bin.cafebabe.syncpart"
    _deep = _sw_root / "uploads" / "sub" / ".c.bin.12345678.syncpart"
    _keep = _sw_root / "uploads" / "normal.bin"
    # 评审 Important：用户自有的非生成物形状不得被误删
    _user_named = _sw_root / "uploads" / "backup.syncpart"
    _user_hidden = _sw_root / "uploads" / ".hidden.syncpart"
    now = time.time()
    for f in (_stale, _fresh, _deep, _keep, _user_named, _user_hidden):
        f.write_bytes(b"x")
    os.utime(_stale, (now - 7200, now - 7200))
    os.utime(_deep, (now - 7200, now - 7200))
    os.utime(_fresh, (now - 60, now - 60))
    os.utime(_user_named, (now - 7200, now - 7200))
    os.utime(_user_hidden, (now - 7200, now - 7200))
    removed = sweep_stale_syncparts(_sw_root, ttl_seconds=3600)
    check("清扫：仅删生成物形状（.名.<8hex>.syncpart）；用户自有 *.syncpart 与新鲜残留保留",
          removed == 2 and not _stale.exists() and not _deep.exists()
          and _fresh.exists() and _keep.exists()
          and _user_named.exists() and _user_hidden.exists(),
          f"removed={removed}")

    # ── normalize_workspace_path（旧附件绝对路径兼容，2026-09-29 波）─────────
    check("归一化：远端绝对路径 → /user_workspaces/<seg>/ 之后部分",
          normalize_workspace_path(
              "/root/weavethinker/user_workspaces/alice-cafef00d/红颊草莓栽培技术资料_中文翻译.docx")
          == "红颊草莓栽培技术资料_中文翻译.docx")
    check("归一化：远端绝对路径（含子目录）",
          normalize_workspace_path(
              "/root/weavethinker/user_workspaces/alice-cafef00d/uploads/646e_文件.pdf")
          == "uploads/646e_文件.pdf")
    check("归一化：相对路径原样通过",
          normalize_workspace_path("uploads/a.bin") == "uploads/a.bin")
    check("归一化：无标记绝对路径 → None（交给调用方按 invalid 处理）",
          normalize_workspace_path("/Users/someone/Library/file.docx") is None)
    check("归一化：归一后含 ../ 越权 → None",
          normalize_workspace_path("/root/w/user_workspaces/u-1/../escape.bin") is None)
    check("归一化：Windows 反斜杠形态也能归一",
          normalize_workspace_path("C:\\root\\w\\user_workspaces\\u-1\\uploads\\a.bin")
          == "uploads/a.bin")
    check("归一化：空/非字符串 → None",
          normalize_workspace_path("") is None and normalize_workspace_path(None) is None)
    check("归一化：相对形态含 marker 的越权串 → None（不得借 marker 放行）",
          normalize_workspace_path("../user_workspaces/u-1/x") is None
          and normalize_workspace_path("a/../user_workspaces/u-1/x") is None)

    # ── 汇总 ────────────────────────────────────────────────────────────────
    failed = [(d, detail) for d, ok, detail in RESULTS if not ok]
    print(f"\n  result: {'PASS' if not failed else 'FAIL'} ({len(RESULTS) - len(failed)}/{len(RESULTS)})")
    assert not failed, f"{len(failed)}/{len(RESULTS)} checks failed: {failed}"


def test_workspace_sync_service_checks():
    """脚本式检查链 pytest 化（wave-2 判据#7）：测试期执行+单例隔离，import 期零副作用。"""
    import shutil as _shutil
    from app.core.config import get_config
    get_config.cache_clear()  # 单例隔离：他模块先缓存的 repo 配置不再污染（原 28/29 真因）
    old_env = os.environ.get("LOCALSTACK_CONFIG_PATH")
    old_weave = os.environ.pop("WEAVE_CONFIG_PATH", None)  # get_config 覆盖链 WEAVE 优先——必须清
    try:
        _run_checks()
    finally:
        if old_env is None:
            os.environ.pop("LOCALSTACK_CONFIG_PATH", None)
        else:
            os.environ["LOCALSTACK_CONFIG_PATH"] = old_env
        if old_weave is not None:
            os.environ["WEAVE_CONFIG_PATH"] = old_weave
        get_config.cache_clear()
        try:
            from app.model_gateway.registry import reload_model_registry
            reload_model_registry()  # 全局注册表复位：防检查链 reload 污染兄弟测试
        except Exception:
            pass
        _shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        test_workspace_sync_service_checks()
    except AssertionError:
        sys.exit(1)
    sys.exit(0)
