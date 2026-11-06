"""可写运行时目录必须落在 config.backend_root（LINUX_DEB_UPSTREAM_FIXES 缺陷 1/2/4）。

背景：冻结/安装形态下 __file__ 位于只读安装目录（如 /opt/WeaveThinker），
audio_files/output_files/.drain_token 用 __file__ 定位会 PermissionError 使
后端启动失败。backend_root 是既定可写基址（打包态=<userData>/localstack/app；
开发态=server/，路径与旧实现逐字节一致）。

配套 read-side 耦合：vision_interpret 解析浏览器截图的 output_files 读侧
（上游 W23 P0-2 新增）与 interactive_browser_service 写侧必须同基址。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_writable_runtime_paths.py -v
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

SERVER_DIR = Path(__file__).resolve().parent.parent
MAIN_PY = SERVER_DIR / "main.py"
ASR_PY = SERVER_DIR / "app" / "services" / "asr_service.py"
EXPORT_PY = SERVER_DIR / "app" / "services" / "export_worker.py"
IBS_PY = SERVER_DIR / "app" / "services" / "interactive_browser_service.py"
VISION_PY = SERVER_DIR / "app" / "tools" / "vision_interpret.py"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_asr_audio_dir_uses_backend_root(tmp_path, monkeypatch):
    """get_audio_files_dir 落 config.backend_root/audio_files（行为腿，RED→GREEN）。"""
    from app.services.asr_service import ASRService

    class _FakeCfg:
        backend_root = tmp_path

    import app.services.asr_service as asr_mod

    monkeypatch.setattr(asr_mod, "get_config", lambda: _FakeCfg())
    svc = object.__new__(ASRService)
    audio_dir = svc.get_audio_files_dir()
    assert audio_dir == (tmp_path / "audio_files").resolve() or audio_dir == tmp_path / "audio_files"
    assert audio_dir.is_dir()


def test_dev_mode_paths_unchanged():
    """开发态钉（缺陷 1/2 的「x3/主文件上溯=server/」同点声明）：asr 行为腿在默认
    dev 配置下必须落 server/audio_files。缺陷 4 类站点（export/ibs/conversation/
    notes）按文档处方**迁移**至 backend_root/output_files（旧 server/app/output_files
    形态作废，读侧 vision_interpret 已同迁）——本测试不为该迁移背书。"""
    from app.core.config import get_config
    from app.services.asr_service import ASRService

    cfg = get_config()
    assert Path(cfg.backend_root).resolve() == SERVER_DIR.resolve()
    svc = object.__new__(ASRService)
    audio_dir = svc.get_audio_files_dir()
    assert audio_dir == (SERVER_DIR / "audio_files").resolve() or audio_dir == SERVER_DIR / "audio_files"


def test_startup_writable_paths_use_backend_root():
    """main.py startup_event：audio/output/.drain_token 基址=backend_root；Fonts 随包只读不动。"""
    src = _src(MAIN_PY)
    assert "data_root = str(config.backend_root)" in src
    assert 'os.path.join(data_root, "audio_files")' in src
    assert 'os.path.join(data_root, "output_files")' in src
    assert 'os.path.join(data_root, ".drain_token")' in src
    # 字体=只读随包资源，保持 __file__ 定位（文档缺陷 1 注）
    assert 'os.path.join(os.path.dirname(__file__), "Fonts")' in src
    # 旧 __file__ 派生可写路径绝迹
    assert 'os.path.join(os.path.dirname(__file__), "audio_files")' not in src
    assert 'os.path.join(os.path.dirname(__file__), "output_files")' not in src
    assert 'os.path.join(os.path.dirname(__file__), ".drain_token")' not in src


def test_output_dirs_use_backend_root():
    """export_worker / interactive_browser_service 写侧 + vision_interpret 读侧同基址。"""
    for path in (EXPORT_PY, IBS_PY):
        src = _src(path)
        assert 'str(get_config().backend_root / "output_files")' in src, path
        assert 'os.path.dirname(os.path.dirname(__file__))' not in src, path
        assert 'os.path.dirname(os.path.dirname(os.path.abspath(__file__)))' not in src, path
    src = _src(VISION_PY)
    # 读侧（browser_screenshot 解析）与写侧同基址
    assert 'str(get_config().backend_root)' in src or 'get_config().backend_root' in src
    assert 'os.path.dirname(os.path.dirname(os.path.abspath(__file__)))' not in src


def test_asr_old_derivation_gone():
    """asr_service 旧 x3 上溯派生绝迹（与行为腿互证）。"""
    src = _src(ASR_PY)
    assert "os.path.dirname(os.path.dirname(os.path.dirname(__file__)))" not in src
    assert 'Path(get_config().backend_root) / "audio_files"' in src


NOTES_PY = SERVER_DIR / "app" / "api" / "notes.py"
CONV_PY = SERVER_DIR / "app" / "api" / "conversation.py"
SKINS_PY = SERVER_DIR / "app" / "api" / "skins.py"


def test_export_api_writable_dirs_use_backend_root():
    """评审 C1/I2 收口：notes/conversation 导出写盘同基址（缺陷 4 类全扫）。
    冻结安装态下 __file__ 派生落只读 /opt → PermissionError=导出功能 500。"""
    for path in (NOTES_PY, CONV_PY):
        src = _src(path)
        assert 'os.path.dirname(os.path.dirname(__file__))' not in src, path
        assert 'os.path.dirname(os.path.dirname(os.path.dirname(__file__)))' not in src, path
        assert 'get_config().backend_root' in src, path
    # notes.py 的 vendor_js 只读资源定位（dirname×2）合法保留——排除后仍须断言 output_files 全改
    assert 'output_dir = str(get_config().backend_root / "output_files")' in _src(NOTES_PY)
    assert 'output_dir = str(get_config().backend_root / "output_files")' in _src(CONV_PY)


def test_skins_custom_dir_uses_backend_root():
    """skins_custom 上传/删除目录落 backend_root（同缺陷类；冻结态只读目录会 500）。"""
    src = _src(SKINS_PY)
    assert 'CUSTOM_SKINS_DIR = str(get_config().backend_root / "skins_custom")' in src \
        or 'CUSTOM_SKINS_DIR = os.path.join(str(get_config().backend_root), "skins_custom")' in src
    assert 'os.path.dirname(os.path.dirname(os.path.dirname(__file__)))' not in src


def test_jobs_root_resolves_against_backend_root():
    """agent_durable_jobs_root 相对值必须锚定 backend_root（W23 冒烟第三暴露：
    默认 'output_files/jobs' cwd 敏感→落 LOCALSTACK_HOME 根的第三棵 output 树）。"""
    from app.core.config import get_config

    cfg = get_config()
    saved = cfg._config.get("agent", {}).get("durable_jobs")
    try:
        cfg._config.setdefault("agent", {})
        cfg._config["agent"]["durable_jobs"] = {}
        assert cfg.agent_durable_jobs_root == str(SERVER_DIR / "output_files" / "jobs")
        cfg._config["agent"]["durable_jobs"] = {"jobs_root": "custom/jobs"}
        assert cfg.agent_durable_jobs_root == str(SERVER_DIR / "custom" / "jobs")
        cfg._config["agent"]["durable_jobs"] = {"jobs_root": "/abs/jobs"}
        assert cfg.agent_durable_jobs_root == "/abs/jobs"
    finally:
        if saved is None:
            cfg._config["agent"].pop("durable_jobs", None)
        else:
            cfg._config["agent"]["durable_jobs"] = saved
