#!/usr/bin/env python3
"""P0/P1 prompt inventory / manifest / coverage / lint (plan 2026-10-18).

Usage (from backend/):
  python -m scripts.prompt_audit inventory
  python -m scripts.prompt_audit coverage --check
  python -m scripts.prompt_audit manifest --out ../docs/prompt_manifest.json
  python -m scripts.prompt_audit lint --check
  python -m scripts.prompt_audit verify --check
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, ".")

ROOT = Path(__file__).resolve().parents[1]  # backend/
APP = ROOT / "app"
_CONFIG_ENV = __import__("os").environ.get("WT_CONFIG_FILE", "").strip()
if _CONFIG_ENV:
    CONFIG_PATH = Path(_CONFIG_ENV).expanduser()
    if not CONFIG_PATH.is_absolute():
        CONFIG_PATH = (ROOT.parent / CONFIG_PATH).resolve()
else:
    CONFIG_PATH = ROOT / "config.toml"
_MANIFEST_PATH = ROOT.parent / "docs" / "prompt_manifest.json"

# System prompts that live in config.toml (not Python literals).  They are
# part of the prompt inventory and must be registered/owned/linted too.
_CONFIG_PROMPT_PATHS = {
    "schedule.execute": ("agent", "scheduled_task_system_prompt"),
    "background.worker": ("agent", "background_task_system_prompt"),
    "voice.duplex": ("voice", "system_prompt"),
}


def _config_prompts() -> List[dict]:
    try:
        import tomllib

        data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for prompt_id, (section, key) in _CONFIG_PROMPT_PATHS.items():
        value = (data.get(section) or {}).get(key)
        if isinstance(value, str) and value.strip():
            out.append({
                "file": "config.toml",
                "line": 0,
                "symbol": f"{section}.{key}",
                "prompt_id": prompt_id,
                "text": value,
            })
    return out


def _is_system_role(node: ast.AST) -> bool:
    if not isinstance(node, ast.Dict):
        return False
    for k, v in zip(node.keys, node.values):
        if isinstance(k, ast.Constant) and k.value == "role":
            return isinstance(v, ast.Constant) and v.value == "system"
    return False


class _SiteVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: List[str] = []
        self.sites: List[dict] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Dict(self, node: ast.Dict) -> None:
        if _is_system_role(node):
            self.sites.append({
                "line": getattr(node, "lineno", 0),
                "symbol": ".".join(self.stack) or "<module>",
            })
        self.generic_visit(node)


def _prompt_id_for(relpath: str, symbol: str) -> str:
    from app.prompts.catalog import prompt_id_for_file

    if relpath == "app/services/agent_loop.py":
        leaf = symbol.rsplit(".", 1)[-1]
        low = leaf.lower()
        full = symbol.lower()
        if leaf.startswith("_coordinate") or "direct" in low or "coordinator_recent" in low:
            return "coordinator.route"
        if "chunked_full" in full:
            return "audit.response"
        if "chunked_claim" in full:
            return "audit.claim_verify"
        if "segmented_claim" in full:
            return "audit.claim_verify"
        if leaf.startswith("_plan_search"):
            return "search.plan"
        if "scout" in low:
            return "audit.scout"
        if "claim" in low:
            return "audit.claim_verify"
        if "citation" in low:
            return "citation.cross_turn"
        if "select_best" in low or "audit" in low:
            return "audit.response"
        return "generic.system"
    if relpath == "app/services/voice_service.py":
        low = symbol.lower()
        if "barge" in low:
            return "voice.barge_in"
        if "interjection" in low:
            return "voice.interjection"
        if "intent" in low:
            return "voice.intent"
        if "endpoint" in low:
            return "voice.endpointing"
        return "voice.duplex"
    if relpath == "app/services/memory_service.py":
        return "memory.dream" if "dream" in symbol.lower() else "memory.summary"
    return prompt_id_for_file(relpath)


def inventory() -> Dict[str, Any]:
    sites: List[dict] = []
    for path in sorted(APP.rglob("*.py")):
        rel = str(path.relative_to(ROOT))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            sites.append({"file": rel, "line": exc.lineno, "error": "syntax"})
            continue
        visitor = _SiteVisitor()
        visitor.visit(tree)
        for site in visitor.sites:
            site["file"] = rel
            site["prompt_id"] = _prompt_id_for(rel, site["symbol"])
            sites.append(site)

    for cfg in _config_prompts():
        sites.append({
            "file": cfg["file"],
            "line": cfg["line"],
            "symbol": cfg["symbol"],
            "prompt_id": cfg["prompt_id"],
            "text_chars": len(cfg["text"]),
        })

    try:
        from app.prompts.registry import all_specs
        specs = {s.id: s for s in all_specs()}
    except Exception as exc:  # pragma: no cover - import diagnostics
        specs = {}
        sites.append({"error": f"registry import failed: {exc}"})

    unmapped = [s for s in sites if s.get("prompt_id") not in specs]
    for site in sites:
        spec = specs.get(site.get("prompt_id") or "")
        if spec is not None:
            site["owner"] = spec.owner
            site["version"] = spec.version
    return {
        "system_sites": sites,
        "system_site_count": sum(1 for s in sites if "line" in s and "file" in s),
        "registered_prompts": [
            {"id": s.id, "version": s.version, "owner": s.owner,
             "used_by": list(s.used_by)}
            for s in specs.values()
        ],
        "registered_count": len(specs),
        "unmapped_count": len(unmapped),
        "unmapped": unmapped,
        "mapped_count": sum(1 for s in sites if s.get("prompt_id") in specs),
        "coverage_note": "every role:system site maps to a registered prompt id",
    }


def lint() -> List[str]:
    from app.prompts.compiler import lint_spec
    from app.prompts.registry import all_specs, deprecation_violations, unused_prompts

    issues: List[str] = list(deprecation_violations(date.today().isoformat()))
    for pid in unused_prompts():
        issues.append(f"{pid}: used_by=[] (dead prompt candidate)")
    _ast_cache = {}

    def _defined_names(path: Path):
        if path not in _ast_cache:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                _ast_cache[path] = set()
            else:
                _ast_cache[path] = {
                    n.name for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                }
        return _ast_cache[path]

    for spec in all_specs():
        for issue in lint_spec(spec):
            issues.append(f"{spec.id}: {issue}")
        for ref in spec.used_by:
            if ref == "config.toml":
                if not CONFIG_PATH.exists():
                    issues.append(f"{spec.id}: used_by config.toml missing")
                continue
            path_s, _, symbol = ref.partition(":")
            path = ROOT / path_s
            if not path.exists():
                issues.append(f"{spec.id}: used_by path missing: {path_s}")
                continue
            if symbol and symbol.rsplit(".", 1)[-1] not in _defined_names(path):
                issues.append(f"{spec.id}: used_by symbol missing: {ref}")

    # C-08: config-owned prompts are a deliberate second editing surface.
    # Keep the route but make ownership/keys executable so a key rename or a
    # missing owner cannot silently orphan the prompt.
    from app.prompts.registry import get as _get_spec

    _config_data = {}
    try:
        import tomllib

        _config_data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        _config_data = {}
    for prompt_id, (section, key) in _CONFIG_PROMPT_PATHS.items():
        spec = _get_spec(prompt_id)
        expected_key = f"{section}.{key}"
        if spec is None:
            issues.append(f"{prompt_id}: config-owned prompt is not registered")
            continue
        if not str(spec.owner or "").strip():
            issues.append(f"{prompt_id}: config-owned prompt has no owner")
        if str((spec.flags or {}).get("config_key") or "") != expected_key:
            issues.append(
                f"{prompt_id}: config_key mismatch "
                f"({(spec.flags or {}).get('config_key')!r} != {expected_key!r})"
            )
        value = (_config_data.get(section) or {}).get(key)
        if not isinstance(value, str) or not value.strip():
            issues.append(f"{prompt_id}: config prompt {expected_key} is empty")

    # AST-level guard for prompt assets that are still inline (migration net).
    from app.prompts.compiler import lint_text
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if len(node.value) < 200:
                    continue
                for issue in lint_text(node.value):
                    issues.append(
                        f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', 0)}: {issue}"
                    )
    for cfg in _config_prompts():
        for issue in lint_text(cfg["text"]):
            issues.append(f"config.toml:{cfg['symbol']}: {issue}")
    return issues


def _manifest_data() -> List[dict]:
    import hashlib

    from app.prompts.registry import all_specs, manifest

    data = manifest()
    specs = {s.id: s for s in all_specs()}
    config_texts = {c["prompt_id"]: c["text"] for c in _config_prompts()}
    for row in data:
        spec = specs.get(row["id"])
        key = (spec.flags or {}).get("config_key") if spec else None
        if key and row["id"] in config_texts:
            text = config_texts[row["id"]]
            row["config_prompt"] = {
                "key": key,
                "chars": len(text),
                "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            }
    return data


def _write_manifest(out: str) -> List[dict]:
    data = _manifest_data()
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return data


def _manifest_drift() -> str:
    import json

    try:
        expected = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"manifest missing/unreadable: {exc}"
    current = _manifest_data()
    if expected != current:
        return f"manifest drift: {_MANIFEST_PATH} is stale (rerun manifest --out)"
    return ""


def _budget_report() -> Dict[str, Any]:
    from app.prompts.budget import check_budget
    from app.prompts.compiler import estimate_tokens
    from app.prompts.registry import all_specs

    per_prompt = {spec.id: estimate_tokens(spec.render()) for spec in all_specs()}
    # ``total_system`` must be the ACTUAL main prompt, not the sum of all
    # alternative prompt specs (the old sum was meaningless).
    actual_system = 0
    try:
        import asyncio
        from types import SimpleNamespace
        from app.prompts.system_agent import build_agent_system_prompt

        text = asyncio.run(build_agent_system_prompt(
            assistant=SimpleNamespace(system_prompt=""),
            shared_context=SimpleNamespace(
                memory_summary="", dream_summary="", memory_entries=[]),
            workspace=SimpleNamespace(root_path="/tmp/prompt-budget"),
            user=SimpleNamespace(id="budget"),
            identity_context="",
            conversation_id="",
            deathmatch_mode=False,
            offered_tool_names=["web_search"],
        ))
        actual_system = estimate_tokens(text)
    except Exception:
        actual_system = 0
    used = {
        "prompt": max(per_prompt.values() or [0]),
        "total_system": actual_system or sum(per_prompt.values()),
    }
    report = check_budget(used, {"prompt": 8000, "total_system": 24000})
    return {
        "used": report.used,
        "limits": report.limits,
        "violations": list(report.violations),
        "per_prompt": per_prompt,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=(
        "inventory", "coverage", "manifest", "lint", "verify", "budget"))
    ap.add_argument("--out", default="")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.command == "budget":
        data = _budget_report()
        print(json.dumps(data, ensure_ascii=False, indent=2))
        if args.check and data["violations"]:
            return 1
        return 0
    if args.command in ("inventory", "coverage"):
        data = inventory()
        if args.command == "inventory":
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        summary = {k: data[k] for k in (
            "system_site_count", "registered_count", "mapped_count",
            "unmapped_count", "unmapped")}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if args.check and data["unmapped_count"]:
            return 1
        return 0
    if args.command == "manifest":
        data = _write_manifest(args.out)
        if args.check:
            drift = _manifest_drift()
            if drift:
                print(drift)
                return 1
        return 0
    issues = lint()
    print("\n".join(issues) if issues else "prompt lint: clean")
    if args.command == "verify":
        data = inventory()
        print(json.dumps({k: data[k] for k in (
            "system_site_count", "registered_count", "mapped_count",
            "unmapped_count")}, ensure_ascii=False, indent=2))
        if data["unmapped_count"]:
            issues.append(f"unmapped prompt sites: {data['unmapped_count']}")
        drift = _manifest_drift()
        if drift:
            issues.append(drift)
    if args.check and issues:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
