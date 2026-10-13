"""随包工具（bin/）受限 PATH 接线回归 — git/ffmpeg 等内置工具在桌面打包形态下可达。

判据 #14/#15 背景：`desktop/local-stack.cjs` 把 `Resources/bin` 注入侧车 PATH
（ffmpeg 已受益），并经 `WEAVE_BUNDLED_BIN` 暴露该目录。但服务端三处**自建受限
PATH** 的 spawn（terminal 工具 / code_execution / process_session）从不继承父
PATH——不接线则随包 git 对 agent 的 terminal 命令完全不可见（影子快照走继承
PATH，不受影响）。

Run from server/:  ../.localstack/venv/bin/python -m pytest tests/test_bundled_tool_path.py -v
"""
import inspect
import os
from types import SimpleNamespace

import pytest

from app.services.process_session_service import ProcessSessionManager


def _build_env_with(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("WEAVE_BUNDLED_BIN", raising=False)
    else:
        monkeypatch.setenv("WEAVE_BUNDLED_BIN", value)
    return ProcessSessionManager()._build_env("/tmp/somecwd")


def test_bundled_bin_leads_restricted_path(monkeypatch):
    env = _build_env_with(monkeypatch, "/Applications/WeaveThinker.app/Contents/Resources/bin")
    parts = env["PATH"].split(":")
    assert parts[0] == "/Applications/WeaveThinker.app/Contents/Resources/bin", (
        "随包 bin/ 必须置于受限 PATH 首位（git/ffmpeg 优先于系统 shim）"
    )
    assert "/usr/bin" in parts and "/bin" in parts, "系统路径不得被挤掉"


def test_unset_bundled_bin_keeps_legacy_path(monkeypatch):
    env = _build_env_with(monkeypatch, None)
    parts = env["PATH"].split(":")
    assert parts[0] != "", "未设置时不得产生空 PATH 段（否则 cwd 相对查找＝安全/行为回归）"
    assert "" not in parts
    assert parts[:2] == [p for p in parts[:2] if p], "原有顺序保持"


def test_bundled_bin_empty_string_is_ignored(monkeypatch):
    env = _build_env_with(monkeypatch, "")
    assert "" not in env["PATH"].split(":"), "空串不得成为 PATH 段"


# ── 单一真源接线：三处受限 PATH 构造器统一走 app.services.git_binary ──


def test_git_binary_is_the_single_source(monkeypatch):
    """`git_binary.bundled_bin_dir` 是随包工具目录的唯一读取点；三处受限 PATH 必须同源。"""
    from app.services import git_binary

    monkeypatch.delenv("WEAVE_BUNDLED_BIN", raising=False)
    assert git_binary.bundled_bin_dir() == "", "未设置时返回空串（调用方据此跳过注入）"
    monkeypatch.setenv("WEAVE_BUNDLED_BIN", "/R/bin")
    assert git_binary.bundled_bin_dir() == "/R/bin"


def test_bundled_git_env_targets_runtime_prefix_triplet():
    """RUNTIME_PREFIX 三点成套（bin/../libexec + bin/../share）——env 兜底与相对定位同向。"""
    import os
    import tempfile

    from app.services.git_binary import _bundled_git_env

    with tempfile.TemporaryDirectory() as td:
        res = os.path.join(td, "Resources")
        os.makedirs(os.path.join(res, "bin"))
        os.makedirs(os.path.join(res, "libexec", "git-core"))
        os.makedirs(os.path.join(res, "share", "git-core", "templates"))
        os.environ["WEAVE_BUNDLED_BIN"] = os.path.join(res, "bin")
        try:
            env = _bundled_git_env()
        finally:
            del os.environ["WEAVE_BUNDLED_BIN"]
    assert env and env["GIT_EXEC_PATH"] == os.path.join(res, "libexec", "git-core")
    assert env["GIT_TEMPLATE_DIR"] == os.path.join(res, "share", "git-core", "templates")


def test_resolve_git_path_finds_bundled_git(monkeypatch):
    import os
    import stat
    import tempfile

    from app.services.git_binary import resolve_git_path

    monkeypatch.setattr("app.services.git_binary.get_config", lambda: SimpleNamespace(git_tool_path=""))
    with tempfile.TemporaryDirectory() as td:
        fake = os.path.join(td, "git")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
        monkeypatch.setenv("WEAVE_BUNDLED_BIN", td)
        monkeypatch.setattr("app.services.git_binary.shutil.which", lambda *_a, **_k: None)
        assert resolve_git_path() == fake


def test_all_three_restricted_paths_use_git_binary():
    """terminal / code_execution / process_session 三处受限 PATH 必须同源接线
    `git_binary.bundled_bin_dir`（本波最初的内联 `WEAVE_BUNDLED_BIN` 读取已统一收口，
    否则又是一次「两条通路参数面漂移」同款缺陷）。"""
    from app.services import code_execution_service as c
    from app.services import process_session_service as p
    from app.tools import terminal as t

    for name, mod in (("terminal", t), ("code_execution", c), ("process_session", p)):
        src = inspect.getsource(mod)
        assert "git_binary" in src and "bundled_bin_dir" in src, (
            f"{name} 受限 PATH 须接线 git_binary.bundled_bin_dir（单一真源）"
        )
        assert 'os.environ.get("WEAVE_BUNDLED_BIN"' not in src, (
            f"{name} 不得再内联读取 WEAVE_BUNDLED_BIN（须走共享解析器，否则再次漂移）"
        )


# ── 打包布局面契约：RUNTIME_PREFIX 相对定位 ⇒ Resources/{bin,libexec,share} 三点成套 ──


def test_electron_builder_maps_runtime_prefix_triplet():
    import pathlib
    import re

    yml = pathlib.Path(__file__).resolve().parents[2] / "electron-builder.yml"
    text = yml.read_text(encoding="utf-8")
    for src, dst in (
        (".cache/git/bundle/bin", "bin"),
        (".cache/git/bundle/libexec", "libexec"),
        (".cache/git/bundle/share", "share"),
    ):
        assert re.search(rf"- from: {re.escape(src)}\s*\n\s*to: {re.escape(dst)}\b", text), (
            f"electron-builder.yml 缺 {src} → {dst}（RUNTIME_PREFIX 靠 ../libexec 与 "
            f"../share 相对定位，三点必须成套，否则 git init 找不到模板/内建）"
        )
    assert "git-libs" not in text, "源码构建零第三方动态库，git-libs 布局应已废弃"
    assert "git.COPYING.txt" in text, "GPL-2.0 COPYING 须随包"


def test_package_script_builds_runtime_prefix_git():
    import pathlib

    sh = pathlib.Path(__file__).resolve().parents[2] / "script/linux/package_macos.sh"
    text = sh.read_text(encoding="utf-8")
    assert "RUNTIME_PREFIX=YesPlease" in text, "须 RUNTIME_PREFIX 构建（整树可搬迁）"
    assert "HAVE_NS_GET_EXECUTABLE_PATH=YesPlease" in text, "macOS 须用 NSGetExecutablePath 定位"
    # 去注释后再断言（脚本注释里以 install_name_tool 作反例说明，会误命中）
    code_only = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "install_name_tool" not in code_only, "零第三方动态库 ⇒ 不得再有 dylib 改写命令"
    assert "codesign" not in code_only, "零第三方动态库 ⇒ 不得再有 ad-hoc 重签命令"
    assert "26c56c296b38c0695b26fa95f475f1d01704d2d38e73465ca30b0b2f5dc789d3" in text, "源码 SHA 须钉死"
