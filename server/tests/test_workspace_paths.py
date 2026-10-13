"""RED tests (A4.8) — workspace 路径安全原语（2026-09-19）。

契约（docs/BACKEND_DESIGN_workspace_preview_folders_20260919.html §2）：
  - resolve_workspace_file / resolve_workspace_dir：只接受用户工作区内的
    目标（界内绝对路径 / 工作区相对路径 / 裸文件名递归搜索）；
  - 越界（绝对路径在外、`..`、符号链接指向界外、兄弟工作区、null 字节等）
    一律抛 WorkspacePathError，绝不返回界外路径；
  - 符号链接按 resolved 目标校验；递归按名搜索跳过 symlink；
  - to_rel_path 输出 POSIX 相对路径（无前导 /）。

Run: cd backend && python -m pytest tests/test_workspace_paths.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.workspace_paths import (  # noqa: E402
    WorkspacePathError,
    find_workspace_file_by_name,
    is_within,
    resolve_workspace_dir,
    resolve_workspace_file,
    to_rel_path,
)


@pytest.fixture()
def ws(tmp_path):
    """用户工作区 + 界外秘密 + 兄弟工作区 + 符号链接陷阱。"""
    root = tmp_path / "weave" / "user-abc12345"
    root.mkdir(parents=True)
    (root / "uploads").mkdir()
    (root / "files" / "nested" / "deep").mkdir(parents=True)
    (root / "files" / "report.docx").write_bytes(b"docx-bytes")
    (root / "uploads" / "photo.png").write_bytes(b"png-bytes")
    (root / "files" / "nested" / "deep" / "data.bin").write_bytes(b"bin-bytes")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top-secret")

    sibling = tmp_path / "weave" / "user-other999"
    sibling.mkdir(parents=True)
    (sibling / "private.txt").write_text("sibling-secret")

    (root / "link_secret.txt").symlink_to(outside / "secret.txt")
    (root / "link_out_dir").symlink_to(outside, target_is_directory=True)
    (root / "link_in_file.txt").symlink_to(root / "files" / "report.docx")
    return root


# ── resolve_workspace_file：合法目标 ──────────────────────────────────────

def test_file_relative_path_resolves(ws):
    got = resolve_workspace_file("files/report.docx", ws)
    assert got == (ws / "files" / "report.docx").resolve()


def test_file_absolute_inside_resolves(ws):
    target = str(ws / "uploads" / "photo.png")
    assert resolve_workspace_file(target, ws) == (ws / "uploads" / "photo.png").resolve()


def test_file_bare_name_recursive(ws):
    assert resolve_workspace_file("data.bin", ws) == (ws / "files" / "nested" / "deep" / "data.bin").resolve()


def test_file_quoted_and_whitespace_normalized(ws):
    assert resolve_workspace_file('  "files/report.docx"  ', ws) == (ws / "files" / "report.docx").resolve()


def test_inside_symlink_to_inside_file_ok(ws):
    assert resolve_workspace_file("link_in_file.txt", ws) == (ws / "files" / "report.docx").resolve()


# ── resolve_workspace_file：越界拒绝 ──────────────────────────────────────

def test_file_absolute_outside_rejected(ws, tmp_path):
    with pytest.raises(WorkspacePathError):
        resolve_workspace_file(str(tmp_path / "outside" / "secret.txt"), ws)


def test_file_dotdot_escape_rejected(ws):
    with pytest.raises(WorkspacePathError):
        resolve_workspace_file("../../outside/secret.txt", ws)


def test_file_traversal_not_substituted_by_same_name(ws):
    # a same-named file inside the workspace must NOT satisfy a traversal
    # target — strict rejection instead of silent substitution (A4.9 I-5)
    (ws / "files" / "secret.txt").write_text("inside")
    with pytest.raises(WorkspacePathError) as ei:
        resolve_workspace_file("../../outside/secret.txt", ws)
    assert ei.value.code == "workspace_path_not_found"


def test_file_path_like_miss_not_substituted(ws):
    # only pure basenames get the recursive search
    (ws / "files" / "nested" / "deep" / "data.bin")  # exists elsewhere
    with pytest.raises(WorkspacePathError):
        resolve_workspace_file("files/nope/data.bin", ws)


def test_file_urlencoded_traversal_is_literal(ws):
    # 不解码：%2e%2e%2f 是普通文件名 → 找不到（而不是穿越）
    with pytest.raises(WorkspacePathError):
        resolve_workspace_file("%2e%2e%2f%2e%2e%2fsecret.txt", ws)


def test_file_sibling_workspace_rejected(ws, tmp_path):
    with pytest.raises(WorkspacePathError):
        resolve_workspace_file(str(tmp_path / "weave" / "user-other999" / "private.txt"), ws)


def test_file_null_byte_rejected(ws):
    with pytest.raises(WorkspacePathError) as ei:
        resolve_workspace_file("files/report.docx\x00.txt", ws)
    assert ei.value.code == "workspace_path_invalid"


def test_file_symlink_outside_rejected(ws):
    with pytest.raises(WorkspacePathError):
        resolve_workspace_file("link_secret.txt", ws)
    with pytest.raises(WorkspacePathError):
        resolve_workspace_file("link_out_dir/secret.txt", ws)


def test_file_empty_rejected(ws):
    with pytest.raises(WorkspacePathError):
        resolve_workspace_file("", ws)
    with pytest.raises(WorkspacePathError):
        resolve_workspace_file("   ", ws)


def test_file_dir_target_rejected(ws):
    with pytest.raises(WorkspacePathError):
        resolve_workspace_file("files", ws)


def test_file_missing_rejected(ws):
    with pytest.raises(WorkspacePathError) as ei:
        resolve_workspace_file("files/nope.txt", ws)
    assert ei.value.code == "workspace_path_not_found"


def test_file_windows_drive_literal(ws):
    # POSIX 上形如 C:\... 的目标是普通相对名 → 找不到；绝不能被当作绝对路径放行
    with pytest.raises(WorkspacePathError):
        resolve_workspace_file("C:\\Windows\\win.ini", ws)


def test_prefix_collision_sibling_not_within(tmp_path):
    root = tmp_path / "user-1"
    root.mkdir()
    sibling = tmp_path / "user-10"
    sibling.mkdir()
    assert is_within(root, root / "a.txt" if False else root)  # root 自身
    assert not is_within(root, sibling / "x.txt")
    assert not is_within(root, sibling)


# ── resolve_workspace_dir / find_by_name / to_rel_path ───────────────────

def test_dir_relative_and_absolute(ws):
    assert resolve_workspace_dir("files/nested", ws) == (ws / "files" / "nested").resolve()
    assert resolve_workspace_dir(str(ws), ws) == ws.resolve()


def test_dir_outside_and_file_rejected(ws, tmp_path):
    with pytest.raises(WorkspacePathError):
        resolve_workspace_dir(str(tmp_path / "outside"), ws)
    with pytest.raises(WorkspacePathError):
        resolve_workspace_dir("files/report.docx", ws)


def test_dir_symlink_outside_rejected(ws):
    with pytest.raises(WorkspacePathError):
        resolve_workspace_dir("link_out_dir", ws)


def test_find_by_name_skips_symlink(ws):
    # data.bin 只有真文件；link 指向界外同名文件时不得命中 symlink
    (ws / "files" / "nested" / "deep" / "secret.txt").unlink(missing_ok=True)
    assert find_workspace_file_by_name("secret.txt", ws) is None
    assert find_workspace_file_by_name("data.bin", ws) == (ws / "files" / "nested" / "deep" / "data.bin").resolve()


def test_to_rel_path_posix(ws):
    assert to_rel_path(ws / "files" / "report.docx", ws) == "files/report.docx"
    assert to_rel_path(ws, ws) == ""
    assert "\\" not in to_rel_path(ws / "uploads" / "photo.png", ws)


def test_to_rel_path_outside_rejected(ws, tmp_path):
    with pytest.raises(WorkspacePathError):
        to_rel_path(tmp_path / "outside" / "secret.txt", ws)
