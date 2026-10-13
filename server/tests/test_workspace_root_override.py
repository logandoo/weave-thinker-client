"""用户级工作区根覆盖（W12 P2#10）— A4.8 test-first。"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import user_preferences as up  # noqa: E402
from app.services.workspace_service import (  # noqa: E402
    default_workspace_root,
    resolve_user_workspace_root,
    workspace_root_override,
)


def test_prefs_merge_preserves_other_keys():
    user = SimpleNamespace(ui_preferences=json.dumps({"skin_id": "mono-brutal"}))
    up.apply_prefs_patch(user, {"workspace_root": "/tmp/ws-a"})
    assert json.loads(user.ui_preferences) == {
        "skin_id": "mono-brutal",
        "workspace_root": "/tmp/ws-a",
    }
    up.apply_prefs_patch(user, {"workspace_root": None})
    assert json.loads(user.ui_preferences) == {"skin_id": "mono-brutal"}


def test_resolve_honors_override_and_falls_back(tmp_path):
    user = SimpleNamespace(id="abcdefgh-1234", username="demo", ui_preferences=None)
    base = default_workspace_root("abcdefgh-1234", "demo")
    assert base.name == "demo-abcdefgh"
    assert resolve_user_workspace_root(user) == base
    assert workspace_root_override(user) is None

    override = tmp_path / "my projects"
    user.ui_preferences = json.dumps({"workspace_root": str(override)})
    assert workspace_root_override(user) == str(override)
    assert resolve_user_workspace_root(user) == override.resolve()

    user.ui_preferences = json.dumps({"workspace_root": 123})
    assert workspace_root_override(user) is None
    assert resolve_user_workspace_root(user) == base
