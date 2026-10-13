"""workspace_write / workspace_edit（P0 原语）— A4.8 test-first。

覆盖：读后写追踪（未读拒/读后变更拒/容量淘汰）、write 新建/覆盖 diff/append/
create_only、edit 精确替换/唯一性/replace_all/未读拒/读后变更拒、路径逃逸与
.git 元数据拒写、写/编辑成功刷新追踪。
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.workspace_file_state import WorkspaceReadTracker  # noqa: E402


def _user(uid="u1", perms=None):
    u = SimpleNamespace(id=uid, username="tester")
    u.agent_permissions = perms or {}
    return u


def _patch_workspace(monkeypatch, root: Path):
    async def _ensure(db, user_id, username=None):
        return SimpleNamespace(root_path=str(root))

    monkeypatch.setattr("app.services.workspace_service.ensure_user_workspace", _ensure)


def _call(tool, args, root, monkeypatch, user=None):
    _patch_workspace(monkeypatch, root)
    return json.loads(asyncio.run(tool(args, user=user or _user(), db=object())))


# ---------------------------------------------------------------- tracker


def test_tracker_rejects_unread(tmp_path):
    t = WorkspaceReadTracker(max_entries=8)
    err = t.check("u1", str(tmp_path / "a.txt"), 0, 0)
    assert err and "workspace_read" in err


def test_tracker_allows_after_read_and_rejects_change(tmp_path):
    t = WorkspaceReadTracker(max_entries=8)
    p = tmp_path / "a.txt"
    p.write_text("x")
    st = p.stat()
    t.record_read("u1", str(p), st.st_mtime_ns, st.st_size)
    assert t.check("u1", str(p), st.st_mtime_ns, st.st_size) is None
    assert t.check("u1", str(p), st.st_mtime_ns + 1, st.st_size)
    assert t.check("u1", str(p), st.st_mtime_ns, st.st_size + 1)


def test_tracker_is_user_scoped(tmp_path):
    t = WorkspaceReadTracker(max_entries=8)
    p = tmp_path / "a.txt"
    p.write_text("x")
    st = p.stat()
    t.record_read("u1", str(p), st.st_mtime_ns, st.st_size)
    assert t.check("u2", str(p), st.st_mtime_ns, st.st_size)


def test_tracker_capacity_eviction(tmp_path):
    t = WorkspaceReadTracker(max_entries=2)
    paths = []
    for i in range(3):
        p = tmp_path / f"f{i}.txt"
        p.write_text("x")
        paths.append(p)
        st = p.stat()
        t.record_read("u1", str(p), st.st_mtime_ns, st.st_size)
    st0 = paths[0].stat()
    assert t.check("u1", str(paths[0]), st0.st_mtime_ns, st0.st_size)  # evicted


# ---------------------------------------------------------------- write


def test_workspace_write_creates_file_with_dirs(tmp_path, monkeypatch):
    from app.tools.workspace_write import workspace_write

    res = _call(workspace_write, {"path": "notes/a.md", "content": "hello\n"}, tmp_path, monkeypatch)
    assert res["success"] is True and res["created"] is True
    assert (tmp_path / "notes" / "a.md").read_text() == "hello\n"
    assert ("snapshot_id" in res) or ("snapshot_error" in res)


def test_workspace_write_overwrite_returns_diff(tmp_path, monkeypatch):
    from app.tools.workspace_write import workspace_write

    (tmp_path / "a.txt").write_text("old line\n")
    res = _call(workspace_write, {"path": "a.txt", "content": "new line\n"}, tmp_path, monkeypatch)
    assert res["success"] is True and res["created"] is False
    assert "-old line" in res["diff"] and "+new line" in res["diff"]


def test_workspace_write_append(tmp_path, monkeypatch):
    from app.tools.workspace_write import workspace_write

    (tmp_path / "a.txt").write_text("one\n")
    res = _call(workspace_write, {"path": "a.txt", "content": "two\n", "mode": "append"}, tmp_path, monkeypatch)
    assert res["success"] is True
    assert (tmp_path / "a.txt").read_text() == "one\ntwo\n"


def test_workspace_write_create_only_rejects_existing(tmp_path, monkeypatch):
    from app.tools.workspace_write import workspace_write

    (tmp_path / "a.txt").write_text("x")
    res = _call(workspace_write, {"path": "a.txt", "content": "y", "mode": "create_only"}, tmp_path, monkeypatch)
    assert "error" in res
    assert (tmp_path / "a.txt").read_text() == "x"


def test_workspace_write_rejects_path_escape(tmp_path, monkeypatch):
    from app.tools.workspace_write import workspace_write

    res = _call(workspace_write, {"path": "../evil.txt", "content": "x"}, tmp_path, monkeypatch)
    assert "error" in res
    assert not (tmp_path.parent / "evil.txt").exists()


def test_workspace_write_rejects_git_metadata(tmp_path, monkeypatch):
    from app.tools.workspace_write import workspace_write

    res = _call(workspace_write, {"path": ".git/config", "content": "x"}, tmp_path, monkeypatch)
    assert "error" in res
    assert not (tmp_path / ".git").exists()


def test_workspace_write_refreshes_read_tracker(tmp_path, monkeypatch):
    from app.tools.workspace_write import workspace_edit, workspace_write

    _call(workspace_write, {"path": "a.txt", "content": "v1\n"}, tmp_path, monkeypatch)
    res = _call(workspace_edit, {"path": "a.txt", "old_string": "v1", "new_string": "v2"}, tmp_path, monkeypatch)
    assert res["success"] is True, res
    assert (tmp_path / "a.txt").read_text() == "v2\n"


# ---------------------------------------------------------------- edit


def test_workspace_edit_requires_prior_read(tmp_path, monkeypatch):
    from app.tools.workspace_write import workspace_edit

    (tmp_path / "a.txt").write_text("hello world\n")
    res = _call(workspace_edit, {"path": "a.txt", "old_string": "world", "new_string": "there"}, tmp_path, monkeypatch)
    assert "error" in res and "workspace_read" in res["error"]


def test_workspace_edit_replaces_after_read(tmp_path, monkeypatch):
    from app.tools.workspace_read import workspace_read
    from app.tools.workspace_write import workspace_edit

    (tmp_path / "a.txt").write_text("hello world\n")
    read_res = _call(workspace_read, {"path": "a.txt"}, tmp_path, monkeypatch)
    assert read_res["success"] is True
    res = _call(workspace_edit, {"path": "a.txt", "old_string": "world", "new_string": "there"}, tmp_path, monkeypatch)
    assert res["success"] is True and res["replacements"] == 1
    assert (tmp_path / "a.txt").read_text() == "hello there\n"


def test_workspace_edit_rejects_stale_read(tmp_path, monkeypatch):
    import os

    from app.tools.workspace_read import workspace_read
    from app.tools.workspace_write import workspace_edit

    p = tmp_path / "a.txt"
    p.write_text("hello world\n")
    _call(workspace_read, {"path": "a.txt"}, tmp_path, monkeypatch)
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    res = _call(workspace_edit, {"path": "a.txt", "old_string": "world", "new_string": "there"}, tmp_path, monkeypatch)
    assert "error" in res
    assert p.read_text() == "hello world\n"


def test_workspace_edit_rejects_multiple_matches(tmp_path, monkeypatch):
    from app.tools.workspace_read import workspace_read
    from app.tools.workspace_write import workspace_edit

    p = tmp_path / "a.txt"
    p.write_text("aa\naa\n")
    _call(workspace_read, {"path": "a.txt"}, tmp_path, monkeypatch)
    res = _call(workspace_edit, {"path": "a.txt", "old_string": "aa", "new_string": "bb"}, tmp_path, monkeypatch)
    assert "error" in res and res.get("count") == 2
    assert p.read_text() == "aa\naa\n"


def test_workspace_edit_replace_all(tmp_path, monkeypatch):
    from app.tools.workspace_read import workspace_read
    from app.tools.workspace_write import workspace_edit

    p = tmp_path / "a.txt"
    p.write_text("aa\naa\n")
    _call(workspace_read, {"path": "a.txt"}, tmp_path, monkeypatch)
    res = _call(
        workspace_edit,
        {"path": "a.txt", "old_string": "aa", "new_string": "bb", "replace_all": True},
        tmp_path,
        monkeypatch,
    )
    assert res["success"] is True and res["replacements"] == 2
    assert p.read_text() == "bb\nbb\n"


def test_workspace_edit_zero_match_reports_count(tmp_path, monkeypatch):
    from app.tools.workspace_read import workspace_read
    from app.tools.workspace_write import workspace_edit

    (tmp_path / "a.txt").write_text("hello\n")
    _call(workspace_read, {"path": "a.txt"}, tmp_path, monkeypatch)
    res = _call(workspace_edit, {"path": "a.txt", "old_string": "nope", "new_string": "x"}, tmp_path, monkeypatch)
    assert "error" in res and res.get("count") == 0
