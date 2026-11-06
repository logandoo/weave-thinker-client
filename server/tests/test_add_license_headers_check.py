"""RED→GREEN: `tools/add_license_headers.py --check` 必须能**只读判定**缺头。

背景（2026-10-31 评审 Important #2）：补头脚本没有非零退出路径——不可读文件被
吞成 binary、缺头文件只是「写了」；调用方（opensource_strip）的 rc 检查因此
形同虚设，半成品树（拷完未补头）会被当成成功产物。--check 提供只读断言：
有任何「本应加头但缺头」的文件 → exit 1，且不改动文件。
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "add_license_headers.py"


def _run(dst: Path, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), *flags, str(dst)],
        capture_output=True, text=True, cwd=str(REPO),
    )


def test_check_fails_on_missing_header_and_does_not_write(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("print('hi')\n", encoding="utf-8")
    before = f.read_text(encoding="utf-8")

    proc = _run(tmp_path, "--check")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f.read_text(encoding="utf-8") == before, "--check 不得写文件"


def test_check_passes_when_headers_present(tmp_path):
    proc = _run(tmp_path)  # 正常补齐
    assert proc.returncode == 0, proc.stderr
    f = tmp_path / "a.py"
    f.write_text("print('hi')\n", encoding="utf-8")
    _run(tmp_path)
    assert _run(tmp_path, "--check").returncode == 0


def test_check_ignores_skipped_extensions(tmp_path):
    (tmp_path / "data.json").write_text("{}\n", encoding="utf-8")
    proc = _run(tmp_path, "--check")
    assert proc.returncode == 0, proc.stdout + proc.stderr
