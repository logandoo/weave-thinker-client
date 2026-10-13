"""skills API frontmatter 解析（W12 P0#3）— A4.8 test-first。

回归：`description: |` 块标量曾被朴素解析存成字面 "|"（vibeweaver 实测），
现统一走 skill_tools._parse_frontmatter。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.skills import _parse_skill_md  # noqa: E402


def test_block_scalar_description():
    desc, body = _parse_skill_md(
        "---\nname: v\ndescription: |\n  line one\n  line two\n---\n# BODY\n"
    )
    assert desc == "line one\nline two"
    assert desc != "|"
    assert body == "# BODY\n"


def test_inline_description_and_no_frontmatter():
    desc, body = _parse_skill_md("---\ndescription: hello world\n---\nbody")
    assert desc == "hello world"
    assert body == "body"
    desc2, body2 = _parse_skill_md("no frontmatter here")
    assert desc2 is None
    assert body2 == "no frontmatter here"


def test_folded_scalar_kept_multiline():
    desc, _ = _parse_skill_md(
        "---\ndescription: >\n  fold one\n  fold two\n---\nB"
    )
    assert "fold one" in desc and "fold two" in desc


def test_process_skill_folder_uses_block_description():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from app.api.skills import _process_skill_folder

    md = (
        "---\nname: demo-skill\ndescription: |\n  line one\n  line two\n---\n# BODY\n"
    ).encode()
    files = [("SKILL.md", md), ("scripts/a.py", b"print(1)\n")]
    db = MagicMock()
    exec_res = MagicMock()
    exec_res.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=exec_res)
    db.flush = AsyncMock()
    db.add = MagicMock()
    skill = asyncio.run(_process_skill_folder("demo-skill", files, db, SimpleNamespace(id="u1")))
    assert skill is not None
    assert skill.description == "line one\nline two"
    assert skill.description != "|"
    assert skill.content == "# BODY\n"
    assert db.add.called
