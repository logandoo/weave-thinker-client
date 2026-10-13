"""localstack/import_config.py 模板补齐（W12 P0#4）— A4.8 test-first。"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_SRC = Path(__file__).resolve().parents[2] / "localstack" / "import_config.py"
_spec = importlib.util.spec_from_file_location("localstack_import_config", _SRC)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _template() -> str:
    return mod._TEMPLATE.read_text(encoding="utf-8")


def test_missing_sections_appended_with_template_defaults():
    text = "[server]\nport = 1\n\n[database]\nname = \"x\"\n"
    out = text
    for section in mod._MERGE_SECTIONS:
        out = mod._merge_section(out, _template(), section)
    assert "[agent.skills]" in out
    assert 'force_inject = ["vibeweaver"]' in out
    assert "[hooks]" in out
    assert "[vibeweaver]" in out
    assert "gate_enabled = true" in out


def test_existing_values_preserved_and_missing_keys_inserted():
    text = (
        "[agent.skills]\n"
        "force_inject = [\"other\"]\n"
        "force_inject_max_chars = 1234\n"
    )
    out = mod._merge_section(text, _template(), "agent.skills")
    assert 'force_inject = ["other"]' in out
    assert "force_inject_max_chars = 1234" in out
    assert out.count("[agent.skills]") == 1
    # 模板键已存在 → 不重复插入
    assert out.count("force_inject_max_chars") == 1

    text2 = "[vibeweaver]\nenabled = false\n"
    out2 = mod._merge_section(text2, _template(), "vibeweaver")
    assert "enabled = false" in out2
    assert "gate_enabled = true" in out2  # 缺失键补齐
    assert out2.count("[vibeweaver]") == 1


def test_section_like_string_in_value_does_not_derail():
    text = '[other]\nprompt = "see [vibeweaver] docs"\n'
    out = mod._merge_section(text, _template(), "vibeweaver")
    # 新段必须以行锚定形式追加（不在字符串内部插入键）
    assert "\n[vibeweaver]\n" in out
    assert out.index("\n[vibeweaver]\n") > out.index("prompt")
    assert "gate_enabled = true" in out
    assert out.count("\n[vibeweaver]\n") == 1


def test_section_header_with_trailing_comment_not_duplicated():
    text = "[vibeweaver]  # added by admin\nenabled = false\n"
    out = mod._merge_section(text, _template(), "vibeweaver")
    assert out.count("[vibeweaver]") == 1
    assert "enabled = false" in out
    assert "gate_enabled = true" in out  # 缺失键补齐到既有段内
