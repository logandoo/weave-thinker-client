#!/usr/bin/env python3
"""模型配置随同步 — 单元/集成（standalone；run: .localstack/venv/bin/python server/tests/test_model_config_sync.py）

覆盖：载荷白名单构建（含密钥剥离/深拷贝）/ 客户端落盘（0600、合并保留非模型段、
载荷非白名单段丢弃、幂等、坏载荷拒绝、round-trip 守卫、损坏文件隔离自愈）/
目标路径解析（既有文件 vs 兄弟回退）/ registry 热重载集成。
判据映射：tests/acceptance.md W-M1 判据 1/2/4（含 D-22 评审修复项）。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="mc-sync-test-"))
# registry/目标解析腿：先钉住配置路径再导入 app 模块（get_config 按 env 解析）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import toml  # noqa: E402

from app.services.model_config_sync import (  # noqa: E402
    apply_model_config,
    build_model_config_payload,
    resolve_model_config_target,
)

RESULTS = []


def check(desc: str, ok: bool, detail: str = ""):
    RESULTS.append((desc, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'} {desc}" + (f" ({detail})" if detail else ""))


EP = {"kind": "llm", "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-fake-unit-key-0000",
      "model_name": "mc-probe", "display_name": "MC Probe", "is_custom": True}
PAYLOAD = {"endpoints": {"mc-probe": EP}, "routing": {"main": "mc-probe"}}

def _run_checks():
    os.environ["LOCALSTACK_CONFIG_PATH"] = str(_TMP / "config.toml")
    # ── 判据 1：载荷构建（仅白名单段、深拷贝、密钥剥离开关） ───────────────
    conf = {
        "endpoints": {"a": {"kind": "llm", "base_url": "http://x/v1", "model_name": "m",
                            "api_key": "sk-fake-unit-key-0001"}},
        "routing": {"main": "a"},
        "defaults": {"temperature": 0.7},
        "secrets": {"tavily_api_key": "must-not-ride"},
        "api": {"base_url": "legacy"},
    }
    p = build_model_config_payload(conf)
    check("载荷仅白名单段", set(p) == {"endpoints", "routing", "defaults"}, str(sorted(p)))
    check("无模型段 → 空载荷", build_model_config_payload({"api": {}}) == {})
    check("非 dict 段剔除", build_model_config_payload({"endpoints": "x", "routing": {}}) == {"routing": {}})
    p_nokey = build_model_config_payload(conf, include_keys=False)
    check("include_keys=False 剥离 api_key", "api_key" not in p_nokey["endpoints"]["a"])
    check("剥离不污染源配置（深拷贝）", conf["endpoints"]["a"].get("api_key") == "sk-fake-unit-key-0001")
    check("include_keys=True 保留 api_key", "api_key" in p["endpoints"]["a"])
    conf_nested = {"endpoints": {"a": {
        "api_key": "x", "base_url": "u",
        "extra": {"dashscope_api_key": "y", "keep_me": 1},
        "headers": {"Authorization": "Bearer z", "X-Custom": "ok"},
    }}, "routing": {"main": {"alias": "a", "api_key": "sk-fake-nested-0002"}}}
    p_nested = build_model_config_payload(conf_nested, include_keys=False)
    ep_n = p_nested["endpoints"]["a"]
    check("include_keys=False 深层剥离（extra/headers）",
          "api_key" not in ep_n and "dashscope_api_key" not in ep_n.get("extra", {})
          and "Authorization" not in ep_n.get("headers", {})
          and ep_n["extra"].get("keep_me") == 1 and ep_n["headers"].get("X-Custom") == "ok")
    check("include_keys=False 剥离 routing 嵌套密钥",
          "api_key" not in (p_nested.get("routing", {}).get("main") or {}))

    # ── 判据 2 + 4：客户端落盘 / 合并 / 幂等 / 坏载荷 / 守卫 / 自愈 ─────────
    target = _TMP / "config_model.toml"
    changed = apply_model_config(PAYLOAD, target, source="https://srv-a.example")
    check("fresh 落盘", changed is True and target.exists())
    check("权限 0600", oct(target.stat().st_mode & 0o777) == "0o600", oct(target.stat().st_mode & 0o777))
    loaded = toml.load(target)
    check("round-trip 字段保真",
          loaded["endpoints"]["mc-probe"]["model_name"] == "mc-probe"
          and loaded["endpoints"]["mc-probe"]["base_url"] == EP["base_url"])
    meta_path = target.with_name(target.name + ".sync-meta")
    check("sync-meta 记录来源", meta_path.exists())
    check("sync-meta 权限 0600", oct(meta_path.stat().st_mode & 0o777) == "0o600",
          oct(meta_path.stat().st_mode & 0o777))

    target.write_text('[secrets]\ntavily_api_key = "keep-me"\n[endpoints.old]\nkind = "llm"\n',
                      encoding="utf-8")
    changed = apply_model_config(PAYLOAD, target)
    loaded = toml.load(target)
    check("合并：非模型段保留", loaded.get("secrets", {}).get("tavily_api_key") == "keep-me")
    check("合并：模型段整体替换（old 移除/新别名在位）",
          "old" not in loaded["endpoints"] and "mc-probe" in loaded["endpoints"])
    bak_path = target.with_name(target.name + ".bak-sync")
    check("备份 .bak-sync 存在且 0600",
          bak_path.exists() and oct(bak_path.stat().st_mode & 0o777) == "0o600",
          oct(bak_path.stat().st_mode & 0o777) if bak_path.exists() else "missing")

    content = target.read_bytes()
    changed = apply_model_config(PAYLOAD, target)
    check("幂等：语义无变化不重写", changed is False and target.read_bytes() == content)

    changed = apply_model_config({"endpoints": "not-a-dict"}, target)
    check("坏载荷（段非对象）拒绝", changed is False and target.read_bytes() == content)
    changed = apply_model_config("nope", target)
    check("坏载荷（非对象）拒绝", changed is False and target.read_bytes() == content)

    changed = apply_model_config({"endpoints": {"evil": dict(EP)}, "secrets": {"stolen": "1"}}, target)
    loaded = toml.load(target)
    check("载荷非白名单段丢弃（secrets 不被注入/覆盖）",
          changed is True and loaded.get("secrets", {}).get("tavily_api_key") == "keep-me"
          and "stolen" not in (loaded.get("secrets") or {}))

    content = target.read_bytes()
    changed = apply_model_config({"endpoints": {'a"b': dict(EP)}}, target)
    check("round-trip 守卫：不可解析 TOML 载荷拒绝且不落盘",
          changed is False and target.read_bytes() == content)

    target.write_text("this is not toml [", encoding="utf-8")
    changed = apply_model_config(PAYLOAD, target)
    quarant = list(_TMP.glob("config_model.toml.corrupt-*"))
    check("损坏既有文件：隔离并重建（自愈）",
          changed is True and target.exists() and bool(quarant)
          and quarant[0].read_text(encoding="utf-8") == "this is not toml [")
    check("隔离件权限 0600", bool(quarant) and oct(quarant[0].stat().st_mode & 0o777) == "0o600",
          oct(quarant[0].stat().st_mode & 0o777) if quarant else "missing")
    check("重建后文件可解析", toml.load(target)["endpoints"]["mc-probe"]["model_name"] == "mc-probe")

    # null 值裁剪：恶意载荷 null 不产生重复改写（TOML 无 null）
    changed = apply_model_config({"defaults": {"temperature": None, "top_p": 0.5}}, target)
    content = target.read_bytes()
    changed_again = apply_model_config({"defaults": {"temperature": None, "top_p": 0.5}}, target)
    check("null 裁剪 + 幂等（无重复改写）",
          changed is True and changed_again is False and target.read_bytes() == content
          and "temperature" not in (toml.load(target).get("defaults") or {}))

    # ── 目标路径解析：无 config_model.toml 时回退兄弟路径（安装版场景） ─────
    (_TMP / "config.toml").write_text('[server]\nhost = "127.0.0.1"\nport = 9999\n', encoding="utf-8")
    target.unlink()
    check("resolve 回退=主配置兄弟路径",
          resolve_model_config_target().resolve() == (_TMP / "config_model.toml").resolve(),
          str(resolve_model_config_target()))

    # ── 判据 2 集成腿：落盘 → registry 热重载 → 新别名可见 ─────────────────
    changed = apply_model_config(PAYLOAD, _TMP / "config_model.toml")
    check("集成腿：服务端载荷落盘", changed is True)

    from app.model_gateway.registry import reload_model_registry  # noqa: E402

    reg = reload_model_registry()
    aliases = [ep["alias"] for ep in reg.public_aliases(kind="llm")]
    check("registry 热重载含同步别名", "mc-probe" in aliases, str(aliases))
    ep_reg = reg.get("mc-probe")
    check("registry 端点字段保真", ep_reg.base_url == EP["base_url"] and ep_reg.model_name == "mc-probe")

    # ── 汇总 ────────────────────────────────────────────────────────────────
    failed = [(d, detail) for d, ok, detail in RESULTS if not ok]
    print(f"\n  result: {'PASS' if not failed else 'FAIL'} ({len(RESULTS) - len(failed)}/{len(RESULTS)})")
    assert not failed, f"{len(failed)}/{len(RESULTS)} checks failed: {failed}"


def test_model_config_sync_checks():
    """脚本式检查链 pytest 化（wave-2 判据#7）：测试期执行+单例隔离，import 期零副作用。"""
    import shutil as _shutil
    from app.core.config import get_config
    get_config.cache_clear()  # 单例隔离：他模块先缓存的 repo 配置不再污染（原 28/29 真因）
    old_env = os.environ.get("LOCALSTACK_CONFIG_PATH")
    old_weave = os.environ.pop("WEAVE_CONFIG_PATH", None)  # get_config 覆盖链 WEAVE 优先——必须清
    try:
        _run_checks()
    finally:
        if old_env is None:
            os.environ.pop("LOCALSTACK_CONFIG_PATH", None)
        else:
            os.environ["LOCALSTACK_CONFIG_PATH"] = old_env
        if old_weave is not None:
            os.environ["WEAVE_CONFIG_PATH"] = old_weave
        get_config.cache_clear()
        try:
            from app.model_gateway.registry import reload_model_registry
            reload_model_registry()  # 全局注册表复位：防检查链 reload 污染兄弟测试
        except Exception:
            pass
        _shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        test_model_config_sync_checks()
    except AssertionError:
        sys.exit(1)
    sys.exit(0)
