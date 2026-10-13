"""RED tests (A4.8) — workspace 文件服务：目录列表 + 服务端 zip（2026-09-19）。

契约（docs/BACKEND_DESIGN_workspace_preview_folders_20260919.html §1/§3）：
  - list_directory：dir-first 排序、元数据（size/mtime/type/rel_path）、
    跳过 symlink 与界外项、上限截断（truncated）；
  - build_folder_zip：zip 内根=目录名、含子目录与中文名、逐字节一致、
    symlink/界外项不进包、空目录合法。

Run: cd backend && python -m pytest tests/test_workspace_files_service.py -v
"""
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.workspace_files_service import (  # noqa: E402
    build_folder_zip,
    list_directory,
)


@pytest.fixture()
def ws(tmp_path):
    root = tmp_path / "ws" / "tester-abc12345"
    (root / "files" / "output" / "sub").mkdir(parents=True)
    (root / "files" / "output" / "a.txt").write_text("alpha")
    (root / "files" / "output" / "中文名.md").write_text("# 中文")
    (root / "files" / "output" / "sub" / "c.bin").write_bytes(b"\x00\x01\x02")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    # 界外 symlink 文件 + symlink 目录
    (root / "files" / "output" / "link_secret.txt").symlink_to(outside / "secret.txt")
    (root / "files" / "output" / "link_out").symlink_to(outside, target_is_directory=True)
    return root


def test_list_sorted_dir_first_and_metadata(ws):
    out = list_directory(ws, "files/output")
    names = [e["name"] for e in out["entries"]]
    assert names == ["sub", "a.txt", "中文名.md"]
    sub = out["entries"][0]
    assert sub["is_dir"] is True and sub["type"] == "dir"  # W19 S3：契约规范值 "dir"
    assert sub["rel_path"] == "files/output/sub"
    a = next(e for e in out["entries"] if e["name"] == "a.txt")
    assert a["size"] == 5 and a["type"] == "text"
    assert a["mtime"] > 0
    assert out["path"] == "files/output"
    assert out["truncated"] is False


def test_list_excludes_symlinks(ws):
    out = list_directory(ws, "files/output")
    assert all(e["name"] not in ("link_secret.txt", "link_out") for e in out["entries"])


def test_list_outside_rejected(ws, tmp_path):
    from app.services.workspace_paths import WorkspacePathError
    with pytest.raises(WorkspacePathError):
        list_directory(ws, str(tmp_path / "outside"))


def test_list_file_rejected(ws):
    from app.services.workspace_paths import WorkspacePathError
    with pytest.raises(WorkspacePathError):
        list_directory(ws, "files/output/a.txt")


def test_zip_contents_and_roundtrip(ws, tmp_path):
    dest = tmp_path / "out.zip"
    info = build_folder_zip(ws, "files/output", dest)
    assert info["name"] == "output.zip"
    assert info["file_count"] == 3
    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
    assert names == {"output/a.txt", "output/中文名.md", "output/sub/c.bin"}
    with zipfile.ZipFile(dest) as zf:
        assert zf.read("output/a.txt") == b"alpha"
        assert zf.read("output/sub/c.bin") == b"\x00\x01\x02"
        assert zf.read("output/中文名.md").decode("utf-8") == "# 中文"


def test_zip_excludes_symlinks(ws, tmp_path):
    dest = tmp_path / "out.zip"
    build_folder_zip(ws, "files/output", dest)
    with zipfile.ZipFile(dest) as zf:
        assert not any("link_secret" in n or "link_out" in n for n in zf.namelist())
        assert not any("secret.txt" in n for n in zf.namelist())


def test_zip_empty_dir(ws, tmp_path):
    (ws / "files" / "empty").mkdir()
    dest = tmp_path / "empty.zip"
    info = build_folder_zip(ws, "files/empty", dest)
    assert info["file_count"] == 0
    with zipfile.ZipFile(dest) as zf:
        assert zf.namelist() == []


def test_zip_sanitizes_backslash_names(ws, tmp_path):
    # POSIX allows backslashes in names; Windows extractors treat them as
    # separators → archive must neutralize them (A4.9 zip-slip).
    (ws / "files" / "output" / "evil\\..\\name.txt").write_text("x")
    dest = tmp_path / "out2.zip"
    build_folder_zip(ws, "files/output", dest)
    with zipfile.ZipFile(dest) as zf:
        names = zf.namelist()
    assert not any("\\" in n for n in names)
    assert any("evil_.._name.txt" in n for n in names)


def test_prune_stale_zips_reaps_old_archives(tmp_path, monkeypatch):
    """评审 A5/B5：wt_ws_zip_* 崩溃遗留整包自愈（HTTP /zip 与 fs/zip 共用同一收割面）。"""
    import os
    import tempfile
    import time

    from app.services.workspace_files_service import prune_stale_zips

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    old = tmp_path / "wt_ws_zip_old.zip"
    fresh = tmp_path / "wt_ws_zip_fresh.zip"
    old.write_bytes(b"x")
    fresh.write_bytes(b"y")
    stale_t = time.time() - 7200
    os.utime(old, (stale_t, stale_t))

    prune_stale_zips(max_age_seconds=3600)

    assert not old.exists(), "过期遗留 zip 必须收割"
    assert fresh.exists(), "未过期 zip 不得误删"
