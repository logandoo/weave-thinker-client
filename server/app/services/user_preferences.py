"""users.ui_preferences JSON 读写（合并语义）——W12 P2#10 引入。

历史：skins.py 曾整键覆盖（`{"skin_id": ...}`）；本服务提供合并更新，
避免多设置项（皮肤/工作区根等）互相清空。新调用方一律走本服务。
"""
import json
from typing import Any, Dict

from app.db.database import User


def load_prefs(user: User) -> Dict[str, Any]:
    raw = getattr(user, "ui_preferences", None)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def apply_prefs_patch(user: User, patch: Dict[str, Any]) -> Dict[str, Any]:
    """合并式更新：None 值 = 删除该键；返回更新后的完整偏好字典。"""
    prefs = load_prefs(user)
    for key, value in patch.items():
        if value is None:
            prefs.pop(key, None)
        else:
            prefs[key] = value
    user.ui_preferences = json.dumps(prefs, ensure_ascii=False)
    return prefs
