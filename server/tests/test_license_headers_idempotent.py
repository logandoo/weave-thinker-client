"""tools/add_license_headers.py 幂等性回归（opensource 发布管线）。

Root cause（2026-09-19 发布波实测）：has_spdx 只检查 text[:400]，带超长 YAML
frontmatter 的 SKILL.md（SPDX 头被插入到 >400 字符处）每次运行都会被重复插入
HTML 注释头，污染发行树。
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "add_license_headers.py"


def _run(dst: Path) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [sys.executable, str(TOOL), str(dst)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    assert proc.returncode == 0, proc.stderr
    return proc


def test_long_frontmatter_skill_is_idempotent(tmp_path):
    skill = tmp_path / "SKILL.md"
    long_desc = "desc " * 120  # >400 字符，迫使 SPDX 头落在 text[:400] 之外
    skill.write_text(
        f"---\nname: x\ndescription: {long_desc}\n---\n\n# X\n\nbody\n",
        encoding="utf-8",
    )

    first = _run(tmp_path)
    assert "'added': 1" in first.stdout, first.stdout
    assert skill.read_text(encoding="utf-8").count("SPDX-License-Identifier") == 1

    second = _run(tmp_path)
    assert "'added': 0" in second.stdout, second.stdout
    assert skill.read_text(encoding="utf-8").count("SPDX-License-Identifier") == 1


def test_body_mention_is_not_a_header(tmp_path):
    """A4.9 I1 回归：正文（远低于文件头）提到 SPDX 标记不等于已有 license 头，
    仍须补头；补头后再次运行保持幂等。"""
    doc = tmp_path / "NOTES.md"
    doc.write_text(
        "# Notes\n\n" + "filler\n" * 120 + "See SPDX-License-Identifier docs.\n",
        encoding="utf-8",
    )

    first = _run(tmp_path)
    assert "'added': 1" in first.stdout, first.stdout
    text = doc.read_text(encoding="utf-8")
    assert text.lstrip().startswith("<!-- Copyright")
    assert text.count("SPDX-License-Identifier") == 2  # 头部一处 + 正文原有一处

    second = _run(tmp_path)
    assert "'added': 0" in second.stdout, second.stdout


def test_plain_file_is_idempotent(tmp_path):
    plain = tmp_path / "a.py"
    plain.write_text("print('hi')\n", encoding="utf-8")
    assert "'added': 1" in _run(tmp_path).stdout
    assert "'added': 0" in _run(tmp_path).stdout
    assert plain.read_text(encoding="utf-8").count("SPDX-License-Identifier") == 1
