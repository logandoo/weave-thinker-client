"""workspace_snapshot（P1a 影子 git 快照/回滚）— A4.8 test-first。

覆盖：create→list→restore 内容回滚；快照后新增文件不删除且被列出；
restore 权限门控（workspace_restore 默认拒绝 → _permission_needed）；
写/编辑前自动 capture。
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import workspace_snapshot_service as wss  # noqa: E402


def _user(uid="u1", perms=None):
    u = SimpleNamespace(id=uid, username="tester")
    u.agent_permissions = perms or {}
    return u


def _patch_workspace(monkeypatch, root: Path):
    async def _ensure(db, user_id, username=None):
        return SimpleNamespace(root_path=str(root))

    monkeypatch.setattr("app.services.workspace_service.ensure_user_workspace", _ensure)


@pytest.fixture()
def snapshot_root(tmp_path, monkeypatch):
    base = tmp_path / "_snapshots"
    monkeypatch.setattr(wss, "_snapshots_base_dir", lambda: base)
    return base


def test_snapshot_roundtrip_preserves_newer_files(tmp_path, snapshot_root):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("v1")

    async def scenario():
        created = await wss.create_snapshot("u1", str(ws), reason="unit")
        assert created["ok"], created
        (ws / "a.txt").write_text("v2")
        (ws / "b.txt").write_text("newer")

        listed = await wss.list_snapshots("u1", str(ws))
        assert listed["ok"] and len(listed["snapshots"]) == 1
        assert listed["snapshots"][0]["snapshot_id"] == created["snapshot_id"]

        restored = await wss.restore_snapshot("u1", str(ws), created["snapshot_id"])
        assert restored["ok"], restored
        assert (ws / "a.txt").read_text() == "v1"
        assert (ws / "b.txt").exists()
        assert any("b.txt" in f for f in restored["newer_files_kept"])
        return created["snapshot_id"]

    sid = asyncio.run(scenario())
    assert sid


def test_snapshot_invalid_id_rejected(tmp_path, snapshot_root):
    ws = tmp_path / "ws"
    ws.mkdir()

    async def scenario():
        created = await wss.create_snapshot("u1", str(ws))
        assert created["ok"]
        bad = await wss.restore_snapshot("u1", str(ws), "not-a-snapshot")
        assert bad["ok"] is False and bad.get("error")

    asyncio.run(scenario())


def test_snapshot_large_file_exclusion_is_argv_safe(tmp_path, snapshot_root, monkeypatch):
    """A4.9 I-1 回归：含换行/glob 字符的大文件名不得注入 gitignore 规则
    （旧实现把文件名写进 info/exclude，`big\\n*` 会写入 `*` 清空整个快照树）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "small.txt").write_text("s")
    weird = ws / "big\n*"
    weird.write_bytes(b"x" * 200)
    monkeypatch.setattr(wss, "_max_file_bytes", lambda: 100)

    async def scenario():
        created = await wss.create_snapshot("u1", str(ws))
        assert created["ok"], created
        assert "small.txt\n" not in created.get("skipped_large_files", [])  # sanity
        (ws / "small.txt").unlink()
        restored = await wss.restore_snapshot("u1", str(ws), created["snapshot_id"])
        assert restored["ok"], restored
        assert "small.txt" in restored["restored_paths"]
        assert (ws / "small.txt").read_text() == "s"

    asyncio.run(scenario())


def test_snapshot_excludes_large_files(tmp_path, snapshot_root, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "small.txt").write_text("s")
    big = ws / "big.bin"
    big.write_bytes(b"x" * 200)
    monkeypatch.setattr(wss, "_max_file_bytes", lambda: 100)

    async def scenario():
        created = await wss.create_snapshot("u1", str(ws))
        assert created["ok"] and "big.bin" in created["skipped_large_files"], created
        big.unlink()
        restored = await wss.restore_snapshot("u1", str(ws), created["snapshot_id"])
        assert restored["ok"], restored
        assert (ws / "small.txt").read_text() == "s"
        assert not big.exists()

    asyncio.run(scenario())


def test_snapshot_tool_create_list_restore(tmp_path, monkeypatch, snapshot_root):
    from app.tools.workspace_snapshot import workspace_snapshot

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("v1")
    _patch_workspace(monkeypatch, ws)

    created = json.loads(
        asyncio.run(workspace_snapshot({"action": "create", "label": "before"}, user=_user(), db=object()))
    )
    assert created["success"] is True and created["snapshot_id"]

    (ws / "a.txt").write_text("v2")
    listed = json.loads(asyncio.run(workspace_snapshot({"action": "list"}, user=_user(), db=object())))
    assert listed["success"] is True and len(listed["snapshots"]) == 1

    denied = json.loads(
        asyncio.run(
            workspace_snapshot(
                {"action": "restore", "snapshot_id": created["snapshot_id"]},
                user=_user(),
                db=object(),
            )
        )
    )
    assert denied.get("_permission_needed") is True
    assert denied.get("_permission_key") == "workspace_restore"
    assert (ws / "a.txt").read_text() == "v2"

    granted = json.loads(
        asyncio.run(
            workspace_snapshot(
                {"action": "restore", "snapshot_id": created["snapshot_id"]},
                user=_user(perms={"workspace_restore": True}),
                db=object(),
            )
        )
    )
    assert granted["success"] is True, granted
    assert (ws / "a.txt").read_text() == "v1"


def test_workspace_write_auto_snapshot(tmp_path, monkeypatch, snapshot_root):
    from app.tools.workspace_write import workspace_write

    ws = tmp_path / "ws"
    ws.mkdir()
    _patch_workspace(monkeypatch, ws)

    res = json.loads(
        asyncio.run(
            workspace_write({"path": "a.txt", "content": "v1\n"}, user=_user(), db=object())
        )
    )
    assert res["success"] is True
    assert res.get("snapshot_id"), res

    async def _list():
        return await wss.list_snapshots("u1", str(ws))

    listed = asyncio.run(_list())
    assert listed["ok"] and len(listed["snapshots"]) == 1
