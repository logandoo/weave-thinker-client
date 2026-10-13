"""agent `git` 工具 + git_binary 共享定位 — A4.8 test-first。

覆盖：子命令白名单拒绝（reset/push/checkout…）；危险选项拒绝（-c/--git-dir…）；
越界路径参数拒绝（绝对路径 / `..` 段 / `~`）；只读子命令免审；mutating 走
git_execution（默认允许）；restore 走 workspace_restore（默认拒绝 →
_permission_needed）；commit→rev-parse 哈希往返（全新仓库缺身份自动回退）；
commit 缺 message 拒绝；git 缺失 127 降级；run_git_command NUL 参数 125。
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import git_binary  # noqa: E402
from app.tools import git as git_tool  # noqa: E402


def _user(uid="u1", perms=None):
    u = SimpleNamespace(id=uid, username="tester")
    u.agent_permissions = perms or {}
    return u


def _patch_workspace(monkeypatch, root: Path):
    async def _ensure(db, user_id, username=None):
        return SimpleNamespace(root_path=str(root))

    monkeypatch.setattr("app.services.workspace_service.ensure_user_workspace", _ensure)


def _init_repo(root: Path, with_identity: bool = True):
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    if with_identity:
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=root, check=True)


def _call(args, user=None, workspace=None):
    kwargs = {"user": user if user is not None else _user(), "db": object()}
    if workspace is not None:
        kwargs["workspace_path"] = str(workspace)
    return json.loads(asyncio.run(git_tool.git(args, **kwargs)))


# ---------------- 白名单与参数收容 ----------------

def test_rejects_non_allowlisted_subcommands():
    for bad in ("reset", "push", "checkout", "clean", "config", "remote", ""):
        r = _call({"subcommand": bad})
        assert r["success"] is False and "不支持" in r["error"], r


def test_rejects_dangerous_options():
    for bad in ("-c", "--git-dir=/tmp/x", "--work-tree=/tmp", "-C", "--exec-path=/bin"):
        r = _call({"subcommand": "status", "args": [bad]})
        assert r["success"] is False and "禁止" in r["error"], r


def test_rejects_path_escape_args(tmp_path):
    _init_repo(tmp_path / "ws", with_identity=True)
    ws = tmp_path / "ws"
    (ws / "sub").mkdir()
    for bad in ("../outside.txt", "/etc/passwd", "~/secrets", "a/../../b"):
        r = _call({"subcommand": "diff", "args": [bad]}, workspace=ws)
        assert r["success"] is False and ("越出" in r["error"] or "相对路径" in r["error"]), r


def test_commit_requires_message(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _init_repo(ws)
    _patch_workspace(monkeypatch, ws)
    r = _call({"subcommand": "commit", "message": "   "}, user=_user())
    assert r["success"] is False and "message" in r["error"], r


# ---------------- 只读免审 + 读写正确性 ----------------

def test_status_and_diff_stat_readonly(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _init_repo(ws)
    (ws / "a.txt").write_text("hello\n")
    _patch_workspace(monkeypatch, ws)

    r = _call({"subcommand": "status", "args": ["--short"]})
    assert r["success"] is True and "a.txt" in r["stdout"], r
    assert "git_path" in r and r["exit_code"] == 0

    r2 = _call({"subcommand": "diff", "args": ["--stat"]})
    assert r2["success"] is True, r2


def test_add_commit_revparse_roundtrip(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _init_repo(ws, with_identity=False)  # 身份缺失 → 默认身份回退
    (ws / "a.txt").write_text("v1\n")
    _patch_workspace(monkeypatch, ws)

    r_add = _call({"subcommand": "add", "args": ["-A"]})
    assert r_add["success"] is True, r_add

    r_commit = _call({"subcommand": "commit", "message": "baseline: unit"})
    assert r_commit["success"] is True, r_commit

    r_hash = _call({"subcommand": "rev-parse", "args": ["HEAD"]})
    assert r_hash["success"] is True and len(r_hash["stdout"].strip()) == 40, r_hash

    r_log = _call({"subcommand": "log", "args": ["--oneline"]})
    assert "baseline: unit" in r_log["stdout"], r_log


# ---------------- 权限分级 ----------------

def test_restore_gated_by_workspace_restore_default_deny(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _init_repo(ws)
    _patch_workspace(monkeypatch, ws)
    r = _call({"subcommand": "restore", "args": ["a.txt"]})
    assert r.get("_permission_needed") is True
    assert r["_permission_key"] == "workspace_restore"

    r2 = _call(
        {"subcommand": "restore", "args": ["a.txt"], "_permission_granted": True},
    )
    assert "_permission_needed" not in r2


def test_add_allows_when_git_execution_enabled(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _init_repo(ws)
    (ws / "b.txt").write_text("x\n")
    _patch_workspace(monkeypatch, ws)
    # 默认 git_execution=True → 不请求权限直接执行
    r = _call({"subcommand": "add", "args": ["-A"]})
    assert r["success"] is True and "_permission_needed" not in r, r


def test_add_denied_when_git_execution_disabled(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _init_repo(ws)
    _patch_workspace(monkeypatch, ws)
    r = _call({"subcommand": "add", "args": ["-A"]}, user=_user(perms={"git_execution": False}))
    assert r.get("_permission_needed") is True and r["_permission_key"] == "git_execution"


# ---------------- git_binary 降级语义 ----------------

def test_missing_git_returns_127(monkeypatch, tmp_path):
    monkeypatch.setattr(git_binary, "resolve_git_path", lambda: str(tmp_path / "no-such-git"))
    code, out, err = asyncio.run(
        git_binary.run_git_command(["status"], cwd=str(tmp_path))
    )
    assert code == 127 and "not found" in err


def test_tool_reports_missing_git_gracefully(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _init_repo(ws)
    _patch_workspace(monkeypatch, ws)
    monkeypatch.setattr(git_binary, "resolve_git_path", lambda: str(tmp_path / "no-such-git"))
    monkeypatch.setattr("app.tools.git.resolve_git_path", lambda: str(tmp_path / "no-such-git"), raising=False)
    r = _call({"subcommand": "status"})
    # handler 内部 from ... import resolve_git_path — 直接 patch git_binary 即可
    assert r["success"] is False and r.get("exit_code") == 127, r


def test_nul_argument_returns_125(tmp_path):
    code, _out, err = asyncio.run(
        git_binary.run_git_command(["sta\x00tus"], cwd=str(tmp_path))
    )
    assert code == 125 and "invalid" in err


def test_config_knobs_present():
    from app.core.config import get_config
    cfg = get_config()
    assert cfg.git_tool_enabled in (True, False)
    assert cfg.git_tool_path
    assert cfg.git_tool_timeout > 0


def test_bundled_env_and_resolve(tmp_path, monkeypatch):
    # 模拟打包布局 Resources/{bin,libexec/git-core,share/git-core/templates}
    res = tmp_path / "Resources"
    (res / "bin").mkdir(parents=True)
    (res / "libexec" / "git-core").mkdir(parents=True)
    (res / "share" / "git-core" / "templates").mkdir(parents=True)
    fake_git = res / "bin" / "git"
    fake_git.write_text("#!/bin/sh\nexit 0\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("WEAVE_BUNDLED_BIN", str(res / "bin"))

    env = git_binary._bundled_git_env()
    assert env["GIT_EXEC_PATH"] == str(res / "libexec" / "git-core")
    assert env["GIT_TEMPLATE_DIR"] == str(res / "share" / "git-core" / "templates")
    assert git_binary.bundled_bin_dir() == str(res / "bin")
    # which 解析失败时回退到 WEAVE_BUNDLED_BIN/git
    monkeypatch.setattr("app.services.git_binary.shutil.which", lambda name: None)
    monkeypatch.setattr("app.core.config.get_config", lambda: SimpleNamespace(git_tool_path="definitely-not-a-real-git-name"))
    assert git_binary.resolve_git_path() == str(fake_git)


def test_bundled_env_absent_without_marker():
    import os
    os.environ.pop("WEAVE_BUNDLED_BIN", None)
    assert git_binary._bundled_git_env() is None


# ---------------- A4.9 复审回归（C1 选项值逃逸） ----------------

def test_rejects_option_value_file_primitives(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _init_repo(ws)
    _patch_workspace(monkeypatch, ws)
    # 逃逸/副作用选项必须被本工具拒绝（断言拒绝文案——git 自身的
    # exit 129 不算数，删掉收容规则这些用例必须变红）。
    for bad in (
        "--output=/tmp/evil",
        "--output=../evil",
        "--separate-git-dir=/tmp/evil",
        "--file=/etc/passwd",
        "--template=/tmp/hooks",
        "--pathspec-from-file=/tmp/list",
    ):
        r = _call({"subcommand": "diff", "args": [bad]})
        assert r["success"] is False and "禁止" in r["error"], (bad, r)

    # 缩写形态打在「活性」子命令上（init --template/--separate-git-dir 无
    # 收容时会成功执行 → 这两条钉死前缀拒绝规则本身）。
    for bad in ("--tem=hooks-src", "--sep=gdir"):
        r = _call({"subcommand": "init", "args": [bad]})
        assert r["success"] is False and "禁止" in r["error"], (bad, r)

    # 值分支：选项名合法但 =值 是路径样（独立于名字黑名单）
    r_val = _call({"subcommand": "log", "args": ["--pretty=../evil"]})
    assert r_val["success"] is False and "越界" in r_val["error"], r_val

    # 空格形态（值作为下一 token）同样被路径收容拒绝
    r2 = _call({"subcommand": "diff", "args": ["--output", "/tmp/evil"]})
    assert r2["success"] is False and "禁止" in r2["error"], r2

    # 合法带值选项不受影响（先造一个提交，否则 git log 在空仓库 exit 128）
    (ws / "a.txt").write_text("x\n")
    _call({"subcommand": "add", "args": ["-A"]})
    r_commit = _call({"subcommand": "commit", "message": "seed"})
    assert r_commit["success"] is True, r_commit
    r3 = _call({"subcommand": "log", "args": ["--pretty=oneline"]})
    assert r3["success"] is True, r3
