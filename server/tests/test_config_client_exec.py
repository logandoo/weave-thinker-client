"""[client_exec] 配置段四点同源（PLAN_device_executor §4，键拼写=主方案 §6.5 M-1）。

覆盖：config.py 属性缺省/钳制/键名；config.toml(.example) 段键名一致；
gen_config.py 模板同步（产物含 [client_exec]）。allowed_tools 缺省镜像主方案
§7「走设备」矩阵（双侧白名单 fail-closed 的本侧来源）。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_config_client_exec.py -v
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import get_config  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
ROOT = SERVER_DIR.parent

# 主方案 §6.5/§7 白名单镜像（Consistency Hub：改名先改 Hub 再 grep）。
# 16 名 = 云端 DEFAULT_REMOTE_ALLOWED_TOOLS（chatbot remote_dispatch_router.py，W18 G2 起含
# citation_render/pdf_export）——DEVICE_EXEC_SYNC_W19 S1。
DEFAULT_ALLOWED_TOOLS = [
    "workspace_read", "workspace_write", "workspace_edit", "workspace_glob",
    "grep", "diff", "word_count", "terminal", "process", "git", "execute_code",
    "skill_run_script", "provide_file", "provide_folder",
    "citation_render", "pdf_export",
]

SECTION_KEYS = {
    "enabled", "cloud_url", "heartbeat_seconds",
    "call_timeout_seconds", "call_max_extension_seconds", "max_payload_mb", "allowed_tools",
}


@pytest.fixture()
def cfg():
    config = get_config()
    saved = config._config.get("client_exec")
    try:
        yield config
    finally:
        if saved is None:
            config._config.pop("client_exec", None)
        else:
            config._config["client_exec"] = saved


def test_defaults_match_master_plan(cfg):
    cfg._config.pop("client_exec", None)
    assert cfg.client_exec_enabled is True
    assert cfg.client_exec_cloud_url == ""
    assert cfg.client_exec_heartbeat_seconds == 30
    assert cfg.client_exec_call_timeout_seconds == 120
    assert cfg.client_exec_max_payload_mb == 10
    assert cfg.client_exec_allowed_tools == DEFAULT_ALLOWED_TOOLS
    # 显式空表 = deny-all（fail-closed，评审 B#9）
    cfg._config["client_exec"] = {"allowed_tools": []}
    assert cfg.client_exec_allowed_tools == []


def test_values_honored_and_clamped(cfg):
    cfg._config["client_exec"] = {
        "enabled": False,
        "cloud_url": "https://cloud.example.com/",
        "heartbeat_seconds": 1,
        "call_timeout_seconds": 0,
        "max_payload_mb": 0,
        "allowed_tools": ["terminal"],
    }
    assert cfg.client_exec_enabled is False
    assert cfg.client_exec_cloud_url == "https://cloud.example.com"  # 尾斜杠剥离
    assert cfg.client_exec_heartbeat_seconds == 5  # 下限钳制
    assert cfg.client_exec_call_timeout_seconds == 5
    assert cfg.client_exec_max_payload_mb == 1
    assert cfg.client_exec_allowed_tools == ["terminal"]


def test_bad_values_fall_back_to_defaults(cfg):
    cfg._config["client_exec"] = {
        "heartbeat_seconds": "abc",
        "call_timeout_seconds": None,
        "max_payload_mb": [],
        "allowed_tools": "terminal",
    }
    assert cfg.client_exec_heartbeat_seconds == 30
    assert cfg.client_exec_call_timeout_seconds == 120
    assert cfg.client_exec_max_payload_mb == 10
    assert cfg.client_exec_allowed_tools == DEFAULT_ALLOWED_TOOLS
    # 显式空表 = deny-all（fail-closed，评审 B#9）
    cfg._config["client_exec"] = {"allowed_tools": []}
    assert cfg.client_exec_allowed_tools == []


def _section_block(text: str, section: str) -> str:
    m = re.search(rf"(?m)^\[{re.escape(section)}\]\s*(?:#.*)?$", text)
    assert m, f"[{section}] 段缺失"
    end = re.search(r"(?m)^\[", text[m.end():])
    tail = text[m.start(): m.end() + (end.start() if end else len(text) - m.end())]
    return tail


def test_config_toml_and_example_key_spelling():
    """config.toml.example 的 [client_exec] 键名与主方案 §6.5 一致；实例 config.toml
    （gitignore 的本地生成物）存在时同样核验。"""
    for name in ("config.toml.example", "config.toml"):
        path = SERVER_DIR / name
        if not path.exists():
            assert name == "config.toml", f"{name} 缺失"
            continue
        text = path.read_text(encoding="utf-8")
        block = _section_block(text, "client_exec")
        keys = set(re.findall(r"(?m)^\s*([a-z_]+)\s*=", block))
        assert keys == SECTION_KEYS, f"{name} 键集漂移: {sorted(keys)}"


def test_gen_config_template_sync(tmp_path):
    """gen_config 产物（localstack 落盘路径）必须带 [client_exec] 段（模板同步）。"""
    env = {
        **os.environ,
        "LOCALSTACK_HOME": str(tmp_path),
        "LOCALSTACK_CONFIG_DIR": str(tmp_path),
        "LOCALSTACK_SERVER_DIR": str(SERVER_DIR),
    }
    r = subprocess.run(
        [sys.executable, str(ROOT / "localstack" / "gen_config.py"), "--force"],
        env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    out = (tmp_path / "config.toml").read_text(encoding="utf-8")
    block = _section_block(out, "client_exec")
    assert "allowed_tools" in block and "heartbeat_seconds" in block


def test_call_max_extension_seconds_mirrors_cloud_semantics(cfg):
    """[client_exec] call_max_extension_seconds 镜像云端续期硬上限（补波 3）：
    缺省 3600；0=不限制；非法/负/NaN 回落 3600（不得静默关硬界）。"""
    import math

    cfg._config.pop("client_exec", None)
    assert cfg.client_exec_call_max_extension_seconds == 3600.0
    cfg._config["client_exec"] = {"call_max_extension_seconds": 0}
    assert cfg.client_exec_call_max_extension_seconds == 0.0
    cfg._config["client_exec"] = {"call_max_extension_seconds": 7200}
    assert cfg.client_exec_call_max_extension_seconds == 7200.0
    for bad in ("abc", None, [1], -5, float("nan"), float("inf")):
        cfg._config["client_exec"] = {"call_max_extension_seconds": bad}
        assert cfg.client_exec_call_max_extension_seconds == 3600.0, bad
    assert math.isfinite(cfg.client_exec_call_max_extension_seconds)
