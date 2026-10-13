import json
import re
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.db.database import User, UserWorkspace

config = get_config()


def _slugify(value: Optional[str]) -> str:
    if not value:
        return "user"
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-")
    return slug or "user"


def _workspace_metadata() -> str:
    metadata = {
        "layout": ["files", "artifacts", "scratch"],
        "bootstrap": "project-venv" if config.workspace_use_project_venv else "isolated-dir",
    }
    return json.dumps(metadata, ensure_ascii=False)


def _ensure_workspace_directories(root_path: Path) -> None:
    root_path.mkdir(parents=True, exist_ok=True)
    for child in ("files", "artifacts", "scratch"):
        (root_path / child).mkdir(exist_ok=True)

    if config.workspace_create_readme:
        readme_path = root_path / "README.md"
        if not readme_path.exists():
            readme_path.write_text(
                "# User Workspace\n\n"
                "This workspace is reserved for user-level agent tasks.\n",
                encoding="utf-8",
            )


def default_workspace_root(user_id: str, username: Optional[str] = None) -> Path:
    """默认工作区根：config.workspace_root/<slug>-<uid8>（W12 P2#10 抽出）。"""
    workspace_name = f"{_slugify(username)}-{str(user_id)[:8]}"
    return (config.workspace_root / workspace_name).resolve()


def workspace_root_override(user: Optional[User]) -> Optional[str]:
    """用户级覆盖（ui_preferences.workspace_root；绝对路径）。无/非法 → None。"""
    if user is None:
        return None
    from app.services.user_preferences import load_prefs
    value = load_prefs(user).get("workspace_root")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def resolve_user_workspace_root(user: Optional[User], user_id: str = "", username: Optional[str] = None) -> Path:
    """用户工作区根解析：覆盖优先（绝对路径），否则默认布局。"""
    override = workspace_root_override(user)
    if override:
        try:
            return Path(override).expanduser().resolve()
        except OSError:
            pass
    return default_workspace_root(user_id or str(getattr(user, "id", "") or ""), username or getattr(user, "username", None))


async def ensure_user_workspace(
    db: AsyncSession,
    user_id: str,
    username: Optional[str] = None,
) -> UserWorkspace:
    result = await db.execute(select(UserWorkspace).where(UserWorkspace.user_id == user_id))
    workspace = result.scalar_one_or_none()

    # W12 P2#10：用户级工作区根覆盖（ui_preferences.workspace_root）。
    user_obj: Optional[User] = None
    try:
        res_u = await db.execute(select(User).where(User.id == user_id))
        user_obj = res_u.scalar_one_or_none()
    except Exception:
        user_obj = None
    root_path = resolve_user_workspace_root(user_obj, user_id=user_id, username=username)
    _ensure_workspace_directories(root_path)

    python_env_path = str((config.project_root / ".venv").resolve()) if config.workspace_use_project_venv else None

    if workspace is None:
        workspace = UserWorkspace(
            user_id=user_id,
            root_path=str(root_path),
            python_env_path=python_env_path,
            node_workspace_path=str(root_path),
            metadata_json=_workspace_metadata(),
        )
        db.add(workspace)
        await db.commit()
        await db.refresh(workspace)
        return workspace

    workspace.root_path = str(root_path)
    workspace.python_env_path = python_env_path
    workspace.node_workspace_path = str(root_path)
    if not workspace.metadata_json:
        workspace.metadata_json = _workspace_metadata()
    await db.commit()
    await db.refresh(workspace)
    return workspace