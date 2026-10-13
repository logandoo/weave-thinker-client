#!/usr/bin/env python3
"""脚本式测试 → pytest 可收集（wave-2 判据#7）机械转换器 v3（一次性工具）。

区域=首个非汇总「# ──」段注释 → failed =」；头中的 os.environ
钉点整体移入 _run_checks（防 import 期泄漏污染兄弟测试）；test_* 做
get_config 单例隔离 + env 恢复；保留 __main__ 独立执行。
用法: python3 tools/convert_script_tests.py <file>...
"""
import re
import sys
from pathlib import Path

TAIL_RE = re.compile(r"^failed = \[d for d, ok, _ in RESULTS if not ok\]")
ENV_RE = re.compile(r'^os\.environ\[')


def convert(path: Path) -> None:
    src = path.read_text(encoding="utf-8").splitlines(keepends=True)
    tail_i = next(i for i, l in enumerate(src) if TAIL_RE.match(l))
    region_start = None
    for i in range(tail_i):
        if src[i].startswith("# ──") and "汇总" not in src[i]:
            region_start = i
            break
    assert region_start is not None, f"region not found in {path}"

    head_lines = src[:region_start]
    env_lines = [l for l in head_lines if ENV_RE.match(l)]
    head_lines = [l for l in head_lines if not ENV_RE.match(l)]

    region = [f"    {l}" if l.strip() else l for l in src[region_start:tail_i]]
    stem = path.stem
    suffix = stem[len("test_"):] if stem.startswith("test_") else stem
    test_name = f"test_{suffix}_checks"
    env_block = "".join(f"    {l}" for l in env_lines)

    new_tail = (
        "def _run_checks():\n"
        + env_block
        + "".join(region)
        + "    failed = [(d, detail) for d, ok, detail in RESULTS if not ok]\n"
        + '    print(f"\\n  result: {\'PASS\' if not failed else \'FAIL\'} ({len(RESULTS) - len(failed)}/{len(RESULTS)})")\n'
        + '    assert not failed, f"{len(failed)}/{len(RESULTS)} checks failed: {failed}"\n'
        + "\n\n"
        + f"def {test_name}():\n"
        + '    """脚本式检查链 pytest 化（wave-2 判据#7）：测试期执行+单例隔离，import 期零副作用。"""\n'
        + "    import shutil as _shutil\n"
        + "    from app.core.config import get_config\n"
        + "    get_config.cache_clear()  # 单例隔离：他模块先缓存的 repo 配置不再污染（原 28/29 真因）\n"
        + '    old_env = os.environ.get("LOCALSTACK_CONFIG_PATH")\n'
        + '    old_weave = os.environ.pop("WEAVE_CONFIG_PATH", None)  # get_config 覆盖链 WEAVE 优先——必须清\n'
        + "    try:\n"
        + "        _run_checks()\n"
        + "    finally:\n"
        + "        if old_env is None:\n"
        + '            os.environ.pop("LOCALSTACK_CONFIG_PATH", None)\n'
        + "        else:\n"
        + '            os.environ["LOCALSTACK_CONFIG_PATH"] = old_env\n'
        + '        if old_weave is not None:\n'
        + '            os.environ["WEAVE_CONFIG_PATH"] = old_weave\n'
        + "        get_config.cache_clear()\n"
        + "        try:\n"
        + "            from app.model_gateway.registry import reload_model_registry\n"
        + "            reload_model_registry()  # 全局注册表复位：防检查链 reload 污染兄弟测试\n"
        + "        except Exception:\n"
        + "            pass\n"
        + "        _shutil.rmtree(_TMP, ignore_errors=True)\n"
        + "\n\n"
        + 'if __name__ == "__main__":\n'
        + "    try:\n"
        + f"        {test_name}()\n"
        + "    except AssertionError:\n"
        + "        sys.exit(1)\n"
        + "    sys.exit(0)\n"
    )
    out = "".join(head_lines) + new_tail
    path.write_text(out, encoding="utf-8")
    print(f"converted: {path} (region {region_start + 1}..{tail_i}, test={test_name})")


for arg in sys.argv[1:]:
    convert(Path(arg))
