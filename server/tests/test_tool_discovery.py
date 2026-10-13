#!/usr/bin/env python3
"""工具发现（app.tools.__init__._discover_tools）— standalone 单测。

run: .localstack/venv/bin/python server/tests/test_tool_discovery.py

覆盖（判据映射 tests/acceptance.md 本波判据 2/5）：
1. 冻结文件系统模拟（tools 目录 Path.glob 返回空=PYZ 内无 .py 落盘）→
   发现机制仍须枚举全部工具模块（PyInstaller 冻结下 glob 发现为空=安装版
   tools 完全缺失的根因；iter_modules 不依赖文件系统 glob）。
2. 开发不变式：发现模块集合 == server/app/tools/*.py（排除 __init__/registry）。
3. 注册表含核心工具（terminal/workspace_read/calculate/execute_code）。
"""
import importlib
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "server"))

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'} {name}{(' — ' + detail) if detail else ''}")


def main() -> int:
    import app.tools as at
    from app.tools.registry import registry

    tools_dir = Path(at.__file__).resolve().parent
    expected = {
        p.stem for p in tools_dir.glob("*.py")
        if p.stem not in ("__init__", "registry")
    }

    # 1) 发现机制不得依赖 Path.glob（冻结文件系统语义：PYZ 内无 .py 落盘）。
    #    把 tools 目录的 Path.glob 补丁为空后重新发现——glob 实现会在此归零
    #    （旧实现实测 discovered=0/31）；iter_modules 实现不受影响。
    recorded = []
    real_import = importlib.import_module
    real_glob = Path.glob

    def spy_import(name, *a, **k):
        recorded.append(name)
        return real_import(name, *a, **k)

    def fake_glob(self, pattern):
        if self == tools_dir:
            return iter([])
        return real_glob(self, pattern)

    at._imported = False
    importlib.import_module = spy_import
    Path.glob = fake_glob
    try:
        at._discover_tools()
    finally:
        importlib.import_module = real_import
        Path.glob = real_glob
        at._imported = True

    discovered = {
        n.removeprefix("app.tools.") for n in recorded if n.startswith("app.tools.")
    }
    check("冻结文件系统模拟下仍发现工具模块", len(discovered) > 0,
          f"discovered={len(discovered)}")
    check("冻结模拟发现集 == 磁盘模块集", discovered == expected,
          f"missing={sorted(expected - discovered)} extra={sorted(discovered - expected)}")

    # 2) 开发不变式：正常 import 后注册表含核心工具
    for tool in ("terminal", "workspace_read", "calculate", "execute_code"):
        check(f"注册表含核心工具 {tool}", registry.get_entry(tool) is not None, "")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n  result: {'PASS' if not failed else 'FAIL'} ({len(RESULTS) - len(failed)}/{len(RESULTS)})")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
