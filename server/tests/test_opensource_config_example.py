"""P0 S2: generated public template must be safe by default."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_generated_public_config_forces_super_admin_bypass_false(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("opensource_strip", root / "tools" / "opensource_strip.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    dst = tmp_path / "opensource"
    (dst / "backend").mkdir(parents=True)
    monkeypatch.setattr(mod, "DST", str(dst))
    mod.gen_config_example()

    text = (dst / "backend" / "config.toml.example").read_text(encoding="utf-8")
    assert "super_admin_bypass = false" in text
    assert "super_admin_bypass = true" not in text
