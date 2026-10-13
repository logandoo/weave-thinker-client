"""RED tests (A4.8) — provide_folder + provide_file 无系统路径化（2026-09-19）。

契约（docs/BACKEND_DESIGN_workspace_preview_folders_20260919.html §3）：
  - provide_file 卡片条目 = {name, rel_path, size, type}，不再含绝对 path；
  - provide_folder 卡片条目 = {name, rel_path, size, file_count, type:"folder"}；
  - 两工具只提供用户工作区内目标；越界/不存在 → not_found，不产生卡片。

Run: cd backend && python -m pytest tests/test_provide_folder.py -v
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import workspace_service  # noqa: E402
from app.tools.provide_file import provide_file  # noqa: E402
from app.tools.provide_folder import provide_folder  # noqa: E402

USER = SimpleNamespace(id="user-1", username="tester")


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws" / "tester-abc12345"
    (root / "files" / "output").mkdir(parents=True)
    (root / "files" / "output" / "a.txt").write_text("alpha")
    (root / "files" / "output" / "b.bin").write_bytes(b"12345")
    (root / "files" / "output" / "sub").mkdir()
    (root / "files" / "output" / "sub" / "c.md").write_text("# c")
    (root / "files" / "empty").mkdir()
    (root / "files" / "report.docx").write_bytes(b"docx")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")

    async def _fake_ensure(db, user_id, username=None):
        return SimpleNamespace(root_path=str(root))

    monkeypatch.setattr(workspace_service, "ensure_user_workspace", _fake_ensure)
    return root


# ── provide_file ─────────────────────────────────────────────────────────

def test_provide_file_rel_path_no_absolute(ws):
    out = json.loads(asyncio.run(provide_file({"file_path": "files/report.docx"}, user=USER, db=object())))
    assert out["success"] is True
    (card,) = out["generated_files"]
    assert card["name"] == "report.docx"
    assert card["rel_path"] == "files/report.docx"
    assert card["size"] == 4
    assert "path" not in card
    assert os.path.isabs(card.get("rel_path", "")) is False


def test_provide_file_outside_target_not_found(ws, tmp_path):
    out = json.loads(asyncio.run(provide_file({"file_path": str(tmp_path / "outside" / "secret.txt")}, user=USER, db=object())))
    assert out["success"] is False
    assert out["generated_files"] == []
    assert out["not_found"]


def test_provide_file_bare_name(ws):
    out = json.loads(asyncio.run(provide_file({"files": ["c.md"]}, user=USER, db=object())))
    assert out["success"] is True
    assert out["generated_files"][0]["rel_path"] == "files/output/sub/c.md"


def test_provide_file_missing_user_context():
    out = json.loads(asyncio.run(provide_file({"file_path": "x"}, user=None, db=None)))
    assert "error" in out


# ── provide_folder ───────────────────────────────────────────────────────

def test_provide_folder_card_shape(ws):
    out = json.loads(asyncio.run(provide_folder({"folder_path": "files/output"}, user=USER, db=object())))
    assert out["success"] is True
    (card,) = out["generated_folders"]
    assert card["name"] == "output"
    assert card["rel_path"] == "files/output"
    assert card["type"] == "folder"
    assert card["file_count"] == 3
    assert card["size"] == 5 + 5 + 3
    assert "path" not in card


def test_provide_folder_absolute_inside(ws):
    out = json.loads(asyncio.run(provide_folder({"folder_path": str(ws / "files" / "empty")}, user=USER, db=object())))
    (card,) = out["generated_folders"]
    assert card["rel_path"] == "files/empty"
    assert card["file_count"] == 0
    assert card["size"] == 0


def test_provide_folder_multiple(ws):
    out = json.loads(asyncio.run(provide_folder({"folders": ["files/output", "files/empty"]}, user=USER, db=object())))
    assert [c["rel_path"] for c in out["generated_folders"]] == ["files/output", "files/empty"]


def test_provide_folder_file_target_rejected(ws):
    out = json.loads(asyncio.run(provide_folder({"folder_path": "files/report.docx"}, user=USER, db=object())))
    assert out["success"] is False
    assert out["generated_folders"] == []


def test_provide_folder_outside_rejected(ws, tmp_path):
    out = json.loads(asyncio.run(provide_folder({"folder_path": str(tmp_path / "outside")}, user=USER, db=object())))
    assert out["success"] is False
    assert out["not_found"]


def test_provide_folder_traversal_rejected(ws):
    out = json.loads(asyncio.run(provide_folder({"folder_path": "../../outside"}, user=USER, db=object())))
    assert out["success"] is False
    assert out["generated_folders"] == []


def test_provide_folder_missing_user_context():
    out = json.loads(asyncio.run(provide_folder({"folder_path": "x"}, user=None, db=None)))
    assert "error" in out


def test_provide_folder_root_rejected(ws):
    # the workspace root itself is not a provideable folder card (empty rel_path)
    out = json.loads(asyncio.run(provide_folder({"folder_path": "."}, user=USER, db=object())))
    assert out["success"] is False
    assert out["generated_folders"] == []
    assert out["not_found"]
