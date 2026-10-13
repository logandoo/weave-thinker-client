#!/usr/bin/env python3
"""模型配置两文件合并（2026-08-31）：model_registry.toml → config_model.toml.

背景：端点显式配置曾双份存在于 config_model.toml（legacy 键）与
model_registry.toml（[endpoints.*]/[routing]）。合并后唯一显式来源 =
config_model.toml 的 [endpoints.*]/[routing] 段；registry 运行时先
synthesize（legacy 键 → 等价表，回滚网）再叠加显式段。

两种模式：
  1. 合并模式（model_registry.toml 存在）：把其中的 endpoints/routing 并入
     config_model.toml 尾部，规范改名 deathmatch.vlm → vlm，随后删除
     model_registry.toml。
  2. 再生成模式（model_registry.toml 不存在）：从当前配置 synthesize 出
     endpoints/routing 段写入 config_model.toml（供从未解耦的部署一次性显式化）。

Usage:
    cd backend && python -m scripts.migrate_model_registry           # dry-run
    cd backend && python -m scripts.migrate_model_registry --apply   # 备份+写入+删除 registry
"""
import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import toml

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_config  # noqa: E402
from app.model_gateway.registry import synthesize  # noqa: E402
from app.model_gateway.schemas import ModelEndpoint  # noqa: E402

HEADER = """
# ── 模型端点与路由（2026-08-31 两文件合并，由 migrate_model_registry 并入）──
# [endpoints.*] = 端点唯一显式来源（url/api_key/model_name/默认参数/能力）；
# [routing] = purpose → 别名路由。缺失的端点/路由由 legacy 键（[api]/[providers]/
# [asr]/[voice]/[memory]/[deathmatch.*]）自动合成等价表（回滚网）。
# 改后调用 model_gateway.reload_model_registry() 或重启生效。

"""

RENAMES = {"deathmatch.vlm": "vlm"}  # 规范别名（2026-08-31 VLM 开放）


def _ep_to_doc(ep: ModelEndpoint) -> dict:
    doc = {
        "kind": ep.kind,
        "base_url": ep.base_url,
        "api_key": ep.api_key,
        "model_name": ep.model_name,
        "is_custom": ep.is_custom,
    }
    if ep.provider_type:
        doc["provider_type"] = ep.provider_type
    if ep.display_name:
        doc["display_name"] = ep.display_name
    if ep.params:
        doc["params"] = dict(ep.params)
    if ep.capabilities:
        doc["capabilities"] = dict(ep.capabilities)
    if ep.extra:
        doc["extra"] = dict(ep.extra)
    return doc


def generate_sections_toml(conf: dict) -> str:
    """再生成模式：从配置 synthesize 出 endpoints/routing 段文本。"""
    endpoints, routing = synthesize(conf)
    doc = {
        "endpoints": {alias: _ep_to_doc(ep) for alias, ep in endpoints.items()},
        "routing": dict(routing),
    }
    return toml.dumps(doc)


def _sections_from_registry(registry_path: Path) -> dict:
    """合并模式：读取 model_registry.toml 的 endpoints/routing，规范改名。"""
    data = toml.loads(registry_path.read_text(encoding="utf-8"))
    endpoints = dict(data.get("endpoints") or {})
    routing = dict(data.get("routing") or {})
    renamed: dict = {}
    for old, new in RENAMES.items():
        if old in endpoints and new not in endpoints:
            endpoints[new] = endpoints.pop(old)
            renamed[old] = new
        # 指向被改名端点的 routing 条目改写为新别名（A4.9 W3-M5：删除会把
        # 非 vlm 用途静默打成 main；改写保持显式路由意图）。
        # 仅在改名实际发生时改写（A4.9 W3-R2 deferred edge：两别名并存时
        # routing 指向是显式选择，不得静默重路由）。
        for purpose, target in list(routing.items()):
            alias = target.get("alias") if isinstance(target, dict) else target
            if alias in renamed:
                new = renamed[alias]
                routing[purpose] = (
                    {**target, "alias": new} if isinstance(target, dict) else new
                )
    return {"endpoints": endpoints, "routing": routing}


def build_consolidation(model_path: Path, registry_path: Path):
    """Return (block_text, summary) or raise SystemExit-style ValueError."""
    model_text = model_path.read_text(encoding="utf-8") if model_path.exists() else ""
    parsed = toml.loads(model_text) if model_text.strip() else {}
    if "endpoints" in parsed or "routing" in parsed:
        raise ValueError(
            f"{model_path} 已包含 [endpoints]/[routing] 段 —— 合并只能执行一次。"
            "如需重跑，请先手工移除这些段。"
        )
    if registry_path.exists():
        sections = _sections_from_registry(registry_path)
        mode = "merge"
    else:
        conf = getattr(get_config(), "_config", {}) or {}
        sections = toml.loads(generate_sections_toml(conf))
        mode = "regenerate"
    block = HEADER + toml.dumps(sections)
    return block, {"mode": mode, "endpoints": len(sections["endpoints"]),
                   "routing": len(sections["routing"]), "registry_existed": registry_path.exists()}


def main() -> int:
    parser = argparse.ArgumentParser(description="model_registry.toml → config_model.toml 一次性并入")
    parser.add_argument("--apply", action="store_true", help="实际执行（默认 dry-run）")
    args = parser.parse_args()

    model_path = BACKEND_ROOT / "config_model.toml"
    registry_path = BACKEND_ROOT / "model_registry.toml"
    try:
        block, summary = build_consolidation(model_path, registry_path)
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    print(f"[{'apply' if args.apply else 'dry-run'}] 模式={summary['mode']} "
          f"端点={summary['endpoints']} routing={summary['routing']}")
    doc = toml.loads(block)
    for alias, ep in doc["endpoints"].items():
        print(f"  - {alias} (kind={ep.get('kind')}, model={ep.get('model_name')!r})")
    if not args.apply:
        print(f"[dry-run] 目标: {model_path}（--apply 备份后写入并删除 {registry_path.name}）")
        return 0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = BACKEND_ROOT.parent / "scripts" / "backups" / f"consolidate_{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    if model_path.exists():
        shutil.copy2(model_path, backup_dir / model_path.name)
    if registry_path.exists():
        shutil.copy2(registry_path, backup_dir / registry_path.name)
    with open(model_path, "a", encoding="utf-8") as fh:
        fh.write(block if block.startswith("\n") else "\n" + block)
    if registry_path.exists():
        registry_path.unlink()
    print(f"已并入 {model_path}；备份在 {backup_dir}；{registry_path.name} 已删除")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
