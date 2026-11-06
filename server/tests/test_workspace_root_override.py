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


# ---------------------------------------------------------------------------
# W27 评审 A-Critical 回归腿：上游 no-silent-move 策略不得吞掉 W12 覆盖层的
# 「显式迁移」语义——用户在设置里保存/恢复工作区根后，UserWorkspace 行必须重指
# （root_path/node_workspace_path/python_env_path 三键）；而工具侧 ensure（无显式
# 迁移授权）对租户/用户名漂移保持持久化根（上游策略）。
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeDB:
    def __init__(self, row):
        self.row = row
        self.commits = 0

    async def execute(self, *_a, **_kw):
        return _FakeResult(self.row)

    def add(self, *_a, **_kw):
        pass

    async def commit(self):
        self.commits += 1

    async def refresh(self, *_a, **_kw):
        pass


def _existing_row(root):
    from app.db.database import UserWorkspace

    return UserWorkspace(user_id="abcdefgh-1234", root_path=str(root),
                         python_env_path=None, node_workspace_path=str(root))


def test_explicit_migration_repoints_existing_row(tmp_path):
    """用户显式迁移（设置 API 路径，allow_repoint=True）：覆盖根重指三键。"""
    import asyncio
    import json as _json

    from app.services.workspace_service import ensure_user_workspace

    old_root = tmp_path / "old-ws"
    old_root.mkdir()
    new_root = tmp_path / "new-ws"
    row = _existing_row(old_root)
    db = _FakeDB(row)
    user = SimpleNamespace(id="abcdefgh-1234", username="demo",
                           ui_preferences=_json.dumps({"workspace_root": str(new_root)}))
    out = asyncio.run(ensure_user_workspace(db, user.id, user.username, user=user, allow_repoint=True))
    assert Path(out.root_path).name == "new-ws", "覆盖根必须重指 root_path"
    assert Path(out.node_workspace_path).name == "new-ws", "node_workspace_path 同步重指"
    assert db.commits >= 1


def test_restore_default_repoints_existing_row(tmp_path):
    """恢复默认（override 清空，allow_repoint=True）：行回落默认布局根。"""
    import asyncio

    from app.services.workspace_service import (
        default_workspace_root,
        ensure_user_workspace,
    )

    old_root = tmp_path / "override-ws"
    old_root.mkdir()
    row = _existing_row(old_root)
    db = _FakeDB(row)
    user = SimpleNamespace(id="abcdefgh-1234", username="demo", ui_preferences=None)
    out = asyncio.run(ensure_user_workspace(db, user.id, user.username, user=user, allow_repoint=True))
    assert Path(out.root_path) == default_workspace_root("abcdefgh-1234", "demo"), \
        "恢复默认必须重指回默认布局根"


def test_tool_path_keeps_persisted_root_on_drift(tmp_path):
    """工具侧 ensure（默认不授权迁移）：计算根≠持久化根（租户/用户名漂移）保持持久化根。"""
    import asyncio
    import json as _json

    from app.services.workspace_service import ensure_user_workspace

    old_root = tmp_path / "persisted-ws"
    old_root.mkdir()
    row = _existing_row(old_root)
    db = _FakeDB(row)
    user = SimpleNamespace(id="abcdefgh-1234", username="renamed-user", ui_preferences=None)
    out = asyncio.run(ensure_user_workspace(db, user.id, user.username, user=user))
    assert Path(out.root_path) == old_root, "漂移场景不得静默搬迁（上游 no-silent-move）"
