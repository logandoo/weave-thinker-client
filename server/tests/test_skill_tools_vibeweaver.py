"""vibeweaver 系统技能装载（B2/B3/C2）— A4.8 test-first。

覆盖：frontmatter 块标量（description: |）解析；伴读递归清单（scripts/ 子目录、
深度/数量/内联上限）；skill_view(name, path) 取件与越界拒绝；skill_run_script
系统技能分支（mm_probe --generate 真跑产出 tests/probe_vision.png）与
script_path 逃逸拒绝；force-inject 全文注入 + {VW_DIR} 替换 + 预算截断。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools import skill_tools  # noqa: E402


def test_frontmatter_block_scalar():
    meta, body = skill_tools._parse_frontmatter(
        "---\nname: demo\ndescription: |\n  line one\n  line two\ncategory: system\n---\nBODY"
    )
    assert meta["name"] == "demo"
    assert meta["description"] == "line one\nline two"
    assert meta["category"] == "system"
    assert body == "BODY"


def test_vibeweaver_skill_lists_scripts_recursively():
    async def scenario():
        resolved = await skill_tools.resolve_skill("vibeweaver")
        assert resolved and resolved["source"] == "system"
        paths = [f["path"] for f in resolved.get("files", [])]
        assert "scripts/scan_secrets.py" in paths, paths
        assert "scripts/assert_artifacts.py" in paths
        assert "TESTING_PROTOCOLS.md" in paths
        # 大文件内联省略（列表化后经 path 取件）
        by_path = {f["path"]: f for f in resolved["files"]}
        assert by_path["TESTING_PROTOCOLS.md"].get("content_omitted")
        assert not by_path["scripts/scan_secrets.py"].get("content_omitted")
        return resolved

    asyncio.run(scenario())


def test_skill_view_path_fetch_and_escape():
    async def scenario():
        ok = json.loads(await skill_tools.skill_view(
            {"name": "vibeweaver", "path": "scripts/scan_secrets.py"}
        ))
        assert "import" in ok["content"]
        assert "error" not in ok

        bad = json.loads(await skill_tools.skill_view(
            {"name": "vibeweaver", "path": "../auth.py"}
        ))
        assert "error" in bad

        missing = json.loads(await skill_tools.skill_view(
            {"name": "vibeweaver", "path": "scripts/nope.py"}
        ))
        assert "error" in missing

    asyncio.run(scenario())


def test_skill_run_script_system_mm_probe_produces_evidence(tmp_path):
    """B2 端到端：mm_probe --generate 在工作区根产出 tests/probe_vision.png。"""
    async def scenario():
        ws = tmp_path / "ws"
        ws.mkdir()
        r = json.loads(await skill_tools.skill_run_script(
            {
                "skill_name": "vibeweaver",
                "script_path": "scripts/mm_probe.py",
                "args": "--generate",
                "timeout": 60,
            },
            user=object(),
            workspace_path=str(ws),
        ))
        assert r.get("return_code") == 0, r
        png = ws / "tests" / "probe_vision.png"
        assert png.exists() and png.stat().st_size > 0, r
        assert (ws / "tests" / "probe_vision.expected").exists()

    asyncio.run(scenario())


def test_skill_run_script_rejects_escape(tmp_path):
    async def scenario():
        ws = tmp_path / "ws"
        ws.mkdir()
        r = json.loads(await skill_tools.skill_run_script(
            {
                "skill_name": "vibeweaver",
                "script_path": "../../../etc/passwd",
                "args": "",
            },
            user=object(),
            workspace_path=str(ws),
        ))
        assert "error" in r and "escapes" in r["error"], r

    asyncio.run(scenario())


def test_force_inject_fulltext_and_budget(monkeypatch):
    async def scenario():
        skill_tools._SKILL_PROMPT_CACHE.clear()
        monkeypatch.setattr(
            "app.tools.skill_tools.get_config",
            lambda: type("C", (), {
                "agent_skills_force_inject": ["vibeweaver"],
                "agent_skills_force_inject_max_chars": 60000,
            })(),
        )
        p = await skill_tools.build_skills_system_prompt(None)
        assert '<force_injected_skill name="vibeweaver">' in p
        assert "{VW_DIR}" not in p
        assert "OPERATING COVENANT" in p  # SKILL.md 正文已注入

        skill_tools._SKILL_PROMPT_CACHE.clear()
        monkeypatch.setattr(
            "app.tools.skill_tools.get_config",
            lambda: type("C", (), {
                "agent_skills_force_inject": ["vibeweaver"],
                "agent_skills_force_inject_max_chars": 100,
            })(),
        )
        p2 = await skill_tools.build_skills_system_prompt(None)
        assert '<force_injected_skill' not in p2  # 预算不足 → 不注入

    asyncio.run(scenario())


# ---------------- W12：系统技能目录走资源解析（冻结布局） ----------------

def test_get_skills_dir_uses_resolve_resource(monkeypatch, tmp_path):
    skills_dir = tmp_path / "skills"
    (skills_dir / "vibeweaver").mkdir(parents=True)
    (skills_dir / "vibeweaver" / "SKILL.md").write_text(
        "---\nname: vibeweaver\ndescription: test desc\n---\nbody\n", encoding="utf-8")
    calls = []

    class Cfg:
        def resolve_resource(self, *parts, bundled_first=False):
            calls.append(parts)
            return skills_dir

    monkeypatch.setattr(skill_tools, "config", Cfg())
    assert skill_tools.is_system_skill("vibeweaver")
    assert calls and calls[0] == ("skills",)

    async def scenario():
        names = [s["name"] for s in await skill_tools.list_system_skills()]
        assert "vibeweaver" in names, names

    asyncio.run(scenario())


def test_config_resolve_resource_frozen_priority(monkeypatch, tmp_path):
    from app.core.config import get_config
    cfg = get_config()
    local = tmp_path / "local"
    bundled = tmp_path / "bundle"
    (bundled / "skills").mkdir(parents=True)
    (bundled / "skills" / "x.txt").write_text("bundled", encoding="utf-8")
    cls = type(cfg)
    monkeypatch.setattr(cls, "backend_root", property(lambda self: local))
    monkeypatch.setattr(cls, "resource_root", property(lambda self: bundled))
    assert cfg.resolve_resource("skills") == (bundled / "skills").resolve()
    (local / "skills").mkdir(parents=True)
    (local / "skills" / "x.txt").write_text("local", encoding="utf-8")
    assert cfg.resolve_resource("skills") == (local / "skills").resolve()
    assert cfg.resolve_resource("skills", bundled_first=True) == (bundled / "skills").resolve()


def test_force_inject_delivery_contract_present(monkeypatch):
    """判据#3（2026-10-17，conv 6871cf68 退化出货事故）：force-inject 注入块
    必须携带交付契约——工作流仪式产物（[Verification Gate] 行 / 验收完成表 /
    cap=5 行 / [Convergence] 等）是流程证据，绝不作为用户可见最终回答正文。

    事故：旅行问答被 NPG 误伤打回后，模型把 vibeweaver 的 [Verification Gate]
    完成表当最终回答出货（用户看到仪式表格而非路线答案）。"""
    async def scenario():
        skill_tools._SKILL_PROMPT_CACHE.clear()
        monkeypatch.setattr(
            "app.tools.skill_tools.get_config",
            lambda: type("C", (), {
                "agent_skills_force_inject": ["vibeweaver"],
                "agent_skills_force_inject_max_chars": 60000,
            })(),
        )
        return await skill_tools.build_skills_system_prompt(None)

    p = asyncio.run(scenario())
    assert "交付契约" in p, "注入块缺交付契约（仪式产物不得作为用户可见回答）"
    assert "不得作为用户可见" in p and "最终回答" in p, "交付契约缺核心禁令"
    assert "[Verification Gate]" in p, "交付契约未点名仪式产物"
