"""Config 资源解析器回归（W12 上游同化前置依赖）。

背景：客户端 W12 波（A3 vibeweaver_gate_service / A5 skill_tools）调用
`Config.resolve_resource(...)`——该方法源自客户端 PyInstaller 冻结包波次，
上游此前没有。上游虽从不冻结，但为保持镜像契约 1:1（并防数据目录投毒语义
随镜像进入），将 `resource_root` / `resolve_resource` 移植进上游 Config。

本文件钉住契约：
- 非冻结态 `resource_root == backend_root`（上游恒为此态，行为零变化）；
- 默认 local（backend_root 旁路）优先；`bundled_first=True` 包内副本优先；
- 拒绝绝对路径/.. 穿透；皆无 → backend_root 派生（保留「路径不存在」语义）。

Run from backend/:  python -m pytest tests/test_config_resource_resolver.py -v
"""
from pathlib import Path

import pytest

from app.core.config import Config, clear_config_cache, get_config


# ── resource_root / resolve_resource 存在性与语义 ──


def test_resource_resolver_methods_exist():
    assert hasattr(Config, "resource_root"), "Config.resource_root 缺失（W12 同化前置依赖）"
    assert hasattr(Config, "resolve_resource"), "Config.resolve_resource 缺失（W12 同化前置依赖）"


def test_resource_root_defaults_to_backend_root(tmp_path):
    (tmp_path / "config.toml").write_text("[server]\nhost='127.0.0.1'\n", encoding="utf-8")
    cfg = Config(str(tmp_path / "config.toml"))
    assert cfg.resource_root == cfg.backend_root, "非冻结态 resource_root 须回退 backend_root"


def test_resolve_resource_rejects_traversal(tmp_path):
    (tmp_path / "config.toml").write_text("[server]\nhost='127.0.0.1'\n", encoding="utf-8")
    cfg = Config(str(tmp_path / "config.toml"))
    for bad in ("../escape", "/abs/path"):
        with pytest.raises(ValueError):
            cfg.resolve_resource(bad)


def test_resolve_resource_prefers_local_by_default(tmp_path):
    """默认：backend_root 旁路副本优先（用户覆写/开发语义）。"""
    (tmp_path / "config.toml").write_text("[server]\nhost='127.0.0.1'\n", encoding="utf-8")
    (tmp_path / "res").mkdir()
    (tmp_path / "res" / "x.txt").write_text("local", encoding="utf-8")
    cfg = Config(str(tmp_path / "config.toml"))
    assert cfg.resolve_resource("res", "x.txt") == (tmp_path / "res" / "x.txt").resolve()


def test_resolve_resource_bundled_first_prefers_resource_root(tmp_path, monkeypatch):
    """bundled_first=True：包内副本优先，可写目录不具覆写能力（防数据目录投毒注入）。"""
    (tmp_path / "config.toml").write_text("[server]\nhost='127.0.0.1'\n", encoding="utf-8")
    (tmp_path / "res").mkdir()
    (tmp_path / "res" / "x.txt").write_text("local", encoding="utf-8")
    bundled = tmp_path / "_MEIPASS"
    (bundled / "res").mkdir(parents=True)
    (bundled / "res" / "x.txt").write_text("bundled", encoding="utf-8")

    cfg = Config(str(tmp_path / "config.toml"))
    monkeypatch.setattr(Config, "resource_root", property(lambda self: bundled))
    assert cfg.resolve_resource("res", "x.txt", bundled_first=True) == (bundled / "res" / "x.txt").resolve()
    # 默认序仍取 local（bundled_first 只影响该次调用）
    assert cfg.resolve_resource("res", "x.txt") == (tmp_path / "res" / "x.txt").resolve()


def test_resolve_resource_missing_falls_back_to_backend_derived(tmp_path):
    """皆无 → backend_root 派生（保留开发模式「路径不存在」错误语义）。"""
    (tmp_path / "config.toml").write_text("[server]\nhost='127.0.0.1'\n", encoding="utf-8")
    cfg = Config(str(tmp_path / "config.toml"))
    got = cfg.resolve_resource("nope", "missing.txt")
    assert got == (tmp_path / "nope" / "missing.txt").resolve()
    assert not got.exists(), "缺失路径须如实报缺，不得静默造出不存在的资源"


# ── 配置路径覆盖链（上游面：WEAVE_CONFIG_PATH）──


def test_weave_config_path_wins(tmp_path, monkeypatch):
    a = tmp_path / "a.toml"
    b = tmp_path / "b.toml"
    a.write_text("[server]\nhost='127.0.0.1'\nport=1111\n", encoding="utf-8")
    b.write_text("[server]\nhost='127.0.0.1'\nport=2222\n", encoding="utf-8")
    monkeypatch.setenv("WEAVE_CONFIG_PATH", str(a))
    try:
        cfg = Config()
        assert cfg._config.get("server", {}).get("port") == 1111, "WEAVE_CONFIG_PATH 须优先"
    finally:
        monkeypatch.delenv("WEAVE_CONFIG_PATH", raising=False)


def test_get_config_cache_clear_works(tmp_path):
    """reload_model_registry 依赖 clear_config_cache 生效，否则热重载读旧配置。

    子进程隔离：clear_config_cache 会把进程级单例换新对象，而存量模块级
    `config = get_config()` 持有者（如 memory_cost_governance_service）仍引用
    旧对象——同进程演示会让后续测试的 fixture 补丁打到新对象、服务读旧对象
    （W12 全量回归实测：degrade_steps 5 vs 3 → test_no_escalation_above_max_level 红）。
    """
    import subprocess
    import sys

    a = tmp_path / "a.toml"
    b = tmp_path / "b.toml"
    a.write_text("[server]\nhost='127.0.0.1'\nport=4444\n", encoding="utf-8")
    b.write_text("[server]\nhost='127.0.0.1'\nport=5555\n", encoding="utf-8")
    script = (
        "import os, sys\n"
        f"os.environ['WEAVE_CONFIG_PATH'] = r'{a}'\n"
        "from app.core.config import get_config, clear_config_cache\n"
        "assert get_config()._config.get('server', {}).get('port') == 4444\n"
        f"os.environ['WEAVE_CONFIG_PATH'] = r'{b}'\n"
        "clear_config_cache()\n"
        "assert get_config()._config.get('server', {}).get('port') == 5555, 'clear_config_cache 后须重读配置'\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"子进程断言失败: {proc.stderr[-500:]}"


# ── 本地独有腿（vendor 侧契约，上游无此覆盖变量——保留防 557b108 式回删）──
# desktop/local-stack.cjs 向侧车注入 LOCALSTACK_CONFIG_PATH 指向用户数据目录；
# 上游配置链无此变量，故本腿仅存于 vendor（test-change 登记 W14）。


def test_localstack_config_path_is_the_fallback(tmp_path, monkeypatch):
    """回归锚：`557b108` 曾把 LOCALSTACK_CONFIG_PATH 从覆盖链删掉，打包形态读不到用户配置。"""
    b = tmp_path / "user.toml"
    b.write_text("[server]\nhost='127.0.0.1'\nport=3333\n", encoding="utf-8")
    monkeypatch.delenv("WEAVE_CONFIG_PATH", raising=False)
    monkeypatch.setenv("LOCALSTACK_CONFIG_PATH", str(b))
    try:
        cfg = Config()
        assert cfg.config_path == b.resolve(), (
            "LOCALSTACK_CONFIG_PATH 必须仍被采纳（desktop/local-stack.cjs 向侧车注入此变量）"
        )
        assert cfg._config.get("server", {}).get("port") == 3333
    finally:
        monkeypatch.delenv("LOCALSTACK_CONFIG_PATH", raising=False)


def test_weave_wins_over_localstack(tmp_path, monkeypatch):
    """覆盖链优先级钉：WEAVE_CONFIG_PATH 与 LOCALSTACK_CONFIG_PATH 同设时
    WEAVE 必须胜出（评审 A1：超集替代曾把同设腿丢成两条单设腿，顺序语义失钉）。"""
    a = tmp_path / "a.toml"
    b = tmp_path / "b.toml"
    a.write_text("[server]\nhost='127.0.0.1'\nport=1111\n", encoding="utf-8")
    b.write_text("[server]\nhost='127.0.0.1'\nport=2222\n", encoding="utf-8")
    monkeypatch.setenv("WEAVE_CONFIG_PATH", str(a))
    monkeypatch.setenv("LOCALSTACK_CONFIG_PATH", str(b))
    try:
        cfg = Config()
        assert cfg.config_path == a.resolve()
        assert cfg._config.get("server", {}).get("port") == 1111, "WEAVE_CONFIG_PATH 必须优先于 LOCALSTACK_CONFIG_PATH"
    finally:
        monkeypatch.delenv("WEAVE_CONFIG_PATH", raising=False)
        monkeypatch.delenv("LOCALSTACK_CONFIG_PATH", raising=False)
