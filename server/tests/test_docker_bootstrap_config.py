"""Docker config bootstrap tests (A4.8 test-first).

Run from backend/:  ../.venv/bin/python -m pytest tests/test_docker_bootstrap_config.py -v

Covers docker/bootstrap_config.py: DB overrides, JWT secret generation,
idempotent skip, --force overwrite, and placeholder detection.
"""
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "docker" / "bootstrap_config.py"

EXAMPLE = """\
# 模板（测试夹具）
[server]
host = "0.0.0.0"
port = 8158

[security]
jwt_secret_key = "<YOUR-SECRET>"

[database]
host = "127.0.0.1"
port = 5432
username = "<YOUR-DB-USER>"
password = "<YOUR-SECRET>"
name = "<YOUR-DB-NAME>"
"""


def run_script(*args: str):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=60,
    )


@pytest.fixture()
def example(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml.example"
    p.write_text(EXAMPLE, encoding="utf-8")
    return p


class TestBootstrapConfig:
    def test_applies_database_overrides(self, example, tmp_path):
        out = tmp_path / "config.toml"
        r = run_script("--from", str(example), "--out", str(out),
                       "--db-host", "db", "--db-port", "5432",
                       "--db-user", "u1", "--db-pass", "p1", "--db-name", "n1")
        assert r.returncode == 0, r.stderr
        cfg = tomllib.loads(out.read_text(encoding="utf-8"))
        assert cfg["database"]["host"] == "db"
        assert cfg["database"]["username"] == "u1"
        assert cfg["database"]["password"] == "p1"
        assert cfg["database"]["name"] == "n1"
        assert cfg["server"]["port"] == 8158  # untouched keys preserved

    def test_generates_random_jwt_secret_for_placeholder(self, example, tmp_path):
        out = tmp_path / "config.toml"
        r = run_script("--from", str(example), "--out", str(out))
        assert r.returncode == 0, r.stderr
        cfg = tomllib.loads(out.read_text(encoding="utf-8"))
        secret = cfg["security"]["jwt_secret_key"]
        assert "YOUR-SECRET" not in secret
        assert len(secret) >= 32

    def test_explicit_jwt_secret_respected(self, example, tmp_path):
        out = tmp_path / "config.toml"
        r = run_script("--from", str(example), "--out", str(out),
                       "--jwt-secret", "explicit-secret-value")
        assert r.returncode == 0, r.stderr
        cfg = tomllib.loads(out.read_text(encoding="utf-8"))
        assert cfg["security"]["jwt_secret_key"] == "explicit-secret-value"

    def test_skips_existing_output_without_force(self, example, tmp_path):
        out = tmp_path / "config.toml"
        out.write_text("[server]\nport = 1\n", encoding="utf-8")
        r = run_script("--from", str(example), "--out", str(out),
                       "--db-host", "db")
        assert r.returncode == 0, r.stderr
        assert "skip" in r.stdout.lower()
        assert tomllib.loads(out.read_text(encoding="utf-8"))["server"]["port"] == 1

    def test_force_overwrites_existing_output(self, example, tmp_path):
        out = tmp_path / "config.toml"
        out.write_text("[server]\nport = 1\n", encoding="utf-8")
        r = run_script("--from", str(example), "--out", str(out), "--force",
                       "--db-host", "db")
        assert r.returncode == 0, r.stderr
        cfg = tomllib.loads(out.read_text(encoding="utf-8"))
        assert cfg["database"]["host"] == "db"

    def test_missing_source_fails_cleanly(self, tmp_path):
        r = run_script("--from", str(tmp_path / "nope.toml"),
                       "--out", str(tmp_path / "config.toml"))
        assert r.returncode == 1
        assert "not found" in (r.stderr + r.stdout).lower()

    def test_output_is_a_directory_fails_cleanly(self, example, tmp_path):
        out = tmp_path / "config.toml"
        out.mkdir()
        r = run_script("--from", str(example), "--out", str(out), "--force")
        assert r.returncode == 1
        assert "directory" in (r.stderr + r.stdout).lower()

    def test_preserves_example_comments_and_layout(self, example, tmp_path):
        """生成物保留模板注释与键序（用户要按注释填 key；行级编辑而非 dump）。"""
        out = tmp_path / "config.toml"
        r = run_script("--from", str(example), "--out", str(out), "--db-host", "db")
        assert r.returncode == 0, r.stderr
        text = out.read_text(encoding="utf-8")
        assert "# 模板（测试夹具）" in text
        assert text.index("[server]") < text.index("[security]") < text.index("[database]")

    def test_does_not_require_third_party_toml_module(self, example, tmp_path):
        """stdlib-only：以 -S（不加载 site-packages）运行仍须成功（CI 免装 toml）。"""
        out = tmp_path / "config.toml"
        r = subprocess.run(
            [sys.executable, "-S", str(SCRIPT), "--from", str(example), "--out", str(out)],
            capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, r.stderr
        assert tomllib.loads(out.read_text(encoding="utf-8"))["server"]["port"] == 8158

    def test_missing_section_is_inserted(self, tmp_path):
        """源文件没有 [database]/[security] 段时，覆盖项必须落盘（补插段），不能静默丢弃。"""
        src = tmp_path / "minimal.toml"
        src.write_text('[server]\nhost = "0.0.0.0"\nport = 8158\n', encoding="utf-8")
        out = tmp_path / "config.toml"
        r = run_script("--from", str(src), "--out", str(out),
                       "--db-host", "db", "--db-user", "u1", "--db-pass", "p1",
                       "--db-name", "n1", "--jwt-secret", "s1")
        assert r.returncode == 0, r.stderr
        cfg = tomllib.loads(out.read_text(encoding="utf-8"))
        assert cfg["database"]["host"] == "db"
        assert cfg["database"]["name"] == "n1"
        assert cfg["security"]["jwt_secret_key"] == "s1"

    def test_inline_comment_after_empty_jwt_counts_as_placeholder(self, tmp_path):
        """jwt_secret_key = ""  # 注释 必须识别为空值并生成随机串。"""
        src = tmp_path / "commented.toml"
        src.write_text(
            '[server]\nport = 8158\n\n[security]\n'
            'jwt_secret_key = ""  # 这里还没填\n',
            encoding="utf-8",
        )
        out = tmp_path / "config.toml"
        r = run_script("--from", str(src), "--out", str(out))
        assert r.returncode == 0, r.stderr
        cfg = tomllib.loads(out.read_text(encoding="utf-8"))
        secret = cfg["security"]["jwt_secret_key"]
        assert secret != "" and "#" not in secret and len(secret) >= 32

    def test_inline_comment_after_real_jwt_is_preserved_as_value(self, tmp_path):
        """已填写的 jwt（带行内注释）不得被误判为空值。"""
        src = tmp_path / "filled.toml"
        src.write_text(
            '[server]\nport = 8158\n\n[security]\n'
            'jwt_secret_key = "real-secret-value"  # 已填\n',
            encoding="utf-8",
        )
        out = tmp_path / "config.toml"
        r = run_script("--from", str(src), "--out", str(out))
        assert r.returncode == 0, r.stderr
        cfg = tomllib.loads(out.read_text(encoding="utf-8"))
        assert cfg["security"]["jwt_secret_key"] == "real-secret-value"

    def test_unreadable_output_path_fails_cleanly(self, example, tmp_path):
        """输出路径因父目录不可遍历而 EACCES：须干净报错（含 permission/权限 与处置提示），不得 traceback。"""
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        out = blocked / "config.toml"
        blocked.chmod(0o000)
        try:
            r = run_script("--from", str(example), "--out", str(out), "--force")
        finally:
            blocked.chmod(0o755)
        combined = (r.stdout + r.stderr).lower()
        assert r.returncode == 1, combined
        assert "traceback" not in combined
        assert "permission" in combined or "权限" in combined
        assert "selinux" in combined or "挂载" in combined or ":z" in combined

    def test_unwritable_output_file_fails_cleanly(self, example, tmp_path):
        """输出文件存在但不可写且 --force：须干净报错，不得 traceback。"""
        out = tmp_path / "config.toml"
        out.write_text("[server]\nport = 1\n", encoding="utf-8")
        out.chmod(0o444)
        r = run_script("--from", str(example), "--out", str(out), "--force")
        out.chmod(0o644)
        combined = (r.stdout + r.stderr).lower()
        assert r.returncode == 1, combined
        assert "traceback" not in combined
        assert "permission" in combined or "权限" in combined
