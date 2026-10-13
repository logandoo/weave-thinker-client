#!/usr/bin/env python3
"""只读资源解析器（config.resource_root / config.resolve_resource）— standalone 单测。

run: .localstack/venv/bin/python server/tests/test_resource_resolver.py

覆盖（判据映射 tests/acceptance.md 本波判据 1/2/5 的开发模式腿）：
1. 开发模式：resolve_resource("skills") 解析到 <repo>/server/skills（与现状逐字节一致——不回归契约）。
2. 模拟冻结（sys.frozen=True + sys._MEIPASS=bundle）：backend_root 无 skills、bundle 有 → 解析到 bundle/skills。
3. 优先级：backend_root 与 bundle 同时存在 → backend_root 胜（用户覆写语义）。
4. 皆不存在 → 回退 backend_root 派生（保留开发模式错误信息语义）。
5. 消费点开发平价：skill_tools._get_skills_dir / pdf_fonts Fonts / code_execution_service
   omml_helper 源 / notes vendor_js —— 与改造前现状路径一致（Task 2 完成后本腿转绿）。
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "server"))

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'} {name}{(' — ' + detail) if detail else ''}")


def _make_config(config_path: Path):
    from app.core.config import Config
    return Config(str(config_path))


def _fake_frozen(bundle: Path):
    """模拟 PyInstaller 冻结运行时属性；返回恢复函数。"""
    saved = {k: getattr(sys, k, None) for k in ("frozen", "_MEIPASS")}
    sys.frozen = True  # type: ignore[attr-defined]
    sys._MEIPASS = str(bundle)  # type: ignore[attr-defined]

    def restore():
        for k, v in saved.items():
            if v is None:
                try:
                    delattr(sys, k)
                except AttributeError:
                    pass
            else:
                setattr(sys, k, v)
    return restore


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="resolv-test-"))
    try:
        # 场景准备：config_root（=backend_root 模拟）与 bundle（=_MEIPASS 模拟）
        config_root = tmp / "conf"
        bundle = tmp / "bundle"
        (config_root).mkdir(parents=True)
        (bundle / "skills").mkdir(parents=True)
        (bundle / "Fonts").mkdir(parents=True)
        (bundle / "app" / "vendor_js").mkdir(parents=True)
        (bundle / "app" / "services").mkdir(parents=True)
        (bundle / "app" / "services" / "omml_helper.py").write_text("# helper", encoding="utf-8")
        # 最小 config.toml（toml.load 需要合法文件）
        (config_root / "config.toml").write_text("[server]\nport = 8161\n", encoding="utf-8")

        cfg = _make_config(config_root / "config.toml")

        # 1) 开发模式：解析到 config 旁路资源（无 frozen 属性）
        p = cfg.resolve_resource("skills")
        check("开发模式 resolve_resource('skills') == backend_root/skills",
              p == (config_root / "skills").resolve(), str(p))

        # 2) 开发模式 resource_root == backend_root
        check("开发模式 resource_root == backend_root",
              cfg.resource_root == cfg.backend_root, str(cfg.resource_root))

        # 3) 模拟冻结：backend_root 无 skills → bundle 命中
        restore = _fake_frozen(bundle)
        try:
            p = cfg.resolve_resource("skills")
            check("冻结模拟 resolve_resource('skills') == _MEIPASS/skills",
                  p == (bundle / "skills").resolve(), str(p))
            check("冻结模拟 resource_root == _MEIPASS",
                  cfg.resource_root == bundle.resolve(), str(cfg.resource_root))

            # 4) 优先级：backend_root 也有了 → backend_root 胜（用户覆写）
            (config_root / "skills").mkdir()
            p = cfg.resolve_resource("skills")
            check("冻结模拟 backend_root 覆写优先",
                  p == (config_root / "skills").resolve(), str(p))
            (config_root / "skills").rmdir()

            # 5) 皆不存在 → 回退 backend_root 派生
            p = cfg.resolve_resource("nope_missing")
            check("冻结模拟缺失回退 backend_root",
                  p == (config_root / "nope_missing").resolve(), str(p))

            # 6) 多段路径
            p = cfg.resolve_resource("app", "services", "omml_helper.py")
            check("冻结模拟多段路径 omml_helper.py",
                  p == (bundle / "app" / "services" / "omml_helper.py").resolve(), str(p))
        finally:
            restore()

        # 7) 恢复后开发语义还原
        check("恢复后 resource_root == backend_root",
              cfg.resource_root == cfg.backend_root, "")

        # 8) 消费点开发平价（Task 2 完成后转绿）：真实 server 树
        real_cfg = _make_config(_REPO / "server" / "config.toml")
        from app.tools.skill_tools import _get_system_skills_dir
        check("skill_tools._get_system_skills_dir == server/skills",
              _get_system_skills_dir() == (_REPO / "server" / "skills").resolve(),
              str(_get_system_skills_dir()))

        from app.services import pdf_fonts
        fonts_dir = pdf_fonts.get_fonts_dir() if hasattr(pdf_fonts, "get_fonts_dir") else pdf_fonts.FONTS_DIR
        check("pdf_fonts Fonts 目录 == server/Fonts",
              Path(fonts_dir) == (_REPO / "server" / "Fonts").resolve(), str(fonts_dir))

        from app.services.code_execution_service import _load_omml_helper_source
        src = _load_omml_helper_source()
        check("omml_helper 源可读且非空", len(src) > 100, f"len={len(src)}")

        vj = real_cfg.resolve_resource("app", "vendor_js")
        check("vendor_js 解析 == server/app/vendor_js",
              vj == (_REPO / "server" / "app" / "vendor_js").resolve(), str(vj))
        check("vendor_js 含 echarts.min.js", (vj / "echarts.min.js").exists(), "")

        # 9) 消费点冻结腿（Task 2）：backend_root 无资源 + 模拟冻结 → 命中 bundle 副本
        import app.tools.skill_tools as skill_tools
        import app.services.code_execution_service as ces
        saved_skill_cfg = skill_tools.config
        saved_ces_cfg = ces.config
        skill_tools.config = cfg
        ces.config = cfg
        restore = _fake_frozen(bundle)
        try:
            check("冻结腿 skill_tools._get_system_skills_dir == bundle/skills",
                  skill_tools._get_system_skills_dir() == (bundle / "skills").resolve(),
                  str(skill_tools._get_system_skills_dir()))

            import app.services.skill_evolution_service as ses
            saved_ses_cfg = ses.config
            ses.config = cfg
            try:
                svc = ses.SkillEvolutionService()
                check("冻结腿 skill_evolution._skills_dir == bundle/skills",
                      svc._skills_dir == (bundle / "skills").resolve(),
                      str(svc._skills_dir))
            finally:
                ses.config = saved_ses_cfg

            src = ces._load_omml_helper_source()
            check("冻结腿 omml_helper 源读自 bundle", src.strip() == "# helper", repr(src[:30]))

            (bundle / "skills" / "docx_manipulation").mkdir(parents=True, exist_ok=True)
            (bundle / "skills" / "docx_manipulation" / "MML2OMML.XSL").write_text("<xsl/>", encoding="utf-8")
            xsl = ces._mml2omml_xsl_path()
            check("冻结腿 MML2OMML.XSL == bundle 副本",
                  xsl == str((bundle / "skills" / "docx_manipulation" / "MML2OMML.XSL").resolve()), xsl)
        finally:
            restore()
            skill_tools.config = saved_skill_cfg
            ces.config = saved_ces_cfg

        # 10) 评审修复腿：系统目录(只读)+可写目录合并视图；写路径恒可写目录；
        #     局部本地目录不得遮蔽 bundle（A4.9 R1-Critical/R2-Important）
        import asyncio
        (bundle / "skills" / "sys_a").mkdir(parents=True)
        (bundle / "skills" / "sys_a" / "SKILL.md").write_text("# sys a", encoding="utf-8")
        (bundle / "skills" / "sys_b").mkdir(parents=True)
        (bundle / "skills" / "sys_b" / "SKILL.md").write_text("# sys b", encoding="utf-8")
        (config_root / "skills" / "agent_x").mkdir(parents=True)
        (config_root / "skills" / "agent_x" / "SKILL.md").write_text("# agent x", encoding="utf-8")
        saved_skill_cfg2 = skill_tools.config
        skill_tools.config = cfg
        restore = _fake_frozen(bundle)
        try:
            names = {s["name"] for s in asyncio.run(skill_tools.list_system_skills())}
            check("合并视图：局部本地目录不遮蔽 bundle 系统技能",
                  names == {"sys_a", "sys_b", "agent_x"}, f"names={sorted(names)}")
            check("is_system_skill(bundle 名)=True", skill_tools.is_system_skill("sys_a"))
            check("is_system_skill(可写目录独有)=False", not skill_tools.is_system_skill("agent_x"))

            r = json.loads(asyncio.run(skill_tools.skill_manage(
                {"action": "create", "name": "agent_y", "content": "# y"})))
            check("skill_manage create 成功", r.get("action") == "create", str(r))
            check("create 落可写目录（非 bundle）",
                  (config_root / "skills" / "agent_y" / "SKILL.md").exists()
                  and not (bundle / "skills" / "agent_y").exists(), "")

            r = json.loads(asyncio.run(skill_tools.skill_manage(
                {"action": "delete", "name": "agent_y"})))
            check("delete 可写目录独有可能", r.get("action") == "delete", str(r))
            check("delete 后可写目录已清", not (config_root / "skills" / "agent_y").exists(), "")

            r = json.loads(asyncio.run(skill_tools.skill_manage(
                {"action": "delete", "name": "sys_a"})))
            check("delete bundle 系统技能被拒（守卫与 update 对齐）",
                  "error" in r and (bundle / "skills" / "sys_a" / "SKILL.md").exists(), str(r))

            r = json.loads(asyncio.run(skill_tools.skill_manage(
                {"action": "create", "name": "sys_a", "content": "# hijack"})))
            check("create 撞系统技能名被拒", "error" in r, str(r))
        finally:
            restore()
            skill_tools.config = saved_skill_cfg2

        # 11) bundled_first（omml_helper/MML2OMML 可执行资源：包内优先，防可写覆写注入）
        (config_root / "app" / "services").mkdir(parents=True, exist_ok=True)
        (config_root / "app" / "services" / "omml_helper.py").write_text("# evil", encoding="utf-8")
        restore = _fake_frozen(bundle)
        try:
            p = cfg.resolve_resource("app", "services", "omml_helper.py", bundled_first=True)
            check("bundled_first=True：包内副本胜（可写覆写不生效）",
                  p == (bundle / "app" / "services" / "omml_helper.py").resolve(), str(p))
            p = cfg.resolve_resource("app", "services", "omml_helper.py")
            check("默认（文档化覆写语义）：本地胜",
                  p == (config_root / "app" / "services" / "omml_helper.py").resolve(), str(p))
        finally:
            restore()

        # 12) 拒绝绝对路径/..（foot-gun 护栏）
        for bad in (("/etc",), ("..", "x"), ("a", "../b")):
            try:
                cfg.resolve_resource(*bad)
                check(f"resolve_resource 拒绝 {bad}", False, "no ValueError")
            except ValueError:
                check(f"resolve_resource 拒绝 {bad}", True)

        failed = [n for n, ok, _ in RESULTS if not ok]
        print(f"\n  result: {'PASS' if not failed else 'FAIL'} ({len(RESULTS) - len(failed)}/{len(RESULTS)})")
        return 0 if not failed else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
