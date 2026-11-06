"""C-09: judge calibration must read an explicit judge file and identify it."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.prompt_judge_calibration import _load_predicted_with_meta, load_labels


def test_load_predicted_carries_judge_identity(tmp_path):
    path = tmp_path / "judge.json"
    path.write_text(json.dumps({
        "mode": "live:mimo",
        "judge_alias": "mimo",
        "judge_model": "mimo-v2.6-pro",
        "results": [{"id": "x", "label": "accept"}],
    }), encoding="utf-8")
    predicted, mode, meta = _load_predicted_with_meta(str(path))
    assert predicted == {"x": "accept"}
    assert mode == "live:mimo"
    assert meta["judge_alias"] == "mimo"
    assert meta["judge_model"] == "mimo-v2.6-pro"


def test_judge_file_flag_drives_human_comparison(tmp_path):
    labels = load_labels()
    judge = tmp_path / "judge.json"
    judge.write_text(json.dumps({
        "mode": "live:test-judge",
        "judge_alias": "test-judge",
        "judge_model": "test-model",
        "results": [{"id": r["id"], "label": r["expected"]} for r in labels],
    }), encoding="utf-8")
    human = ROOT / "tests" / "prompt_evals" / "judge_labels_human.json"
    if not human.exists():
        import pytest

        pytest.skip("human labels not present")
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.prompt_judge_calibration",
         "--judge-file", str(judge), "--human", str(human)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "test-judge" in proc.stdout
    assert "test-model" in proc.stdout
