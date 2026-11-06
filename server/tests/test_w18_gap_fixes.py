"""W18 盲区补齐 — ui_preferences 合并 / multi-worker 自检 / media_type（RED 先行）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_w18_gap_fixes.py -v
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.db.database import Base, ClientExecutorDevice, User


class _Bridge:
    def __init__(self, sync_sess):
        self._s = sync_sess

    async def execute(self, stmt):
        return self._s.execute(stmt)

    async def commit(self):
        self._s.commit()

    async def rollback(self):
        self._s.rollback()

    def add(self, obj):
        self._s.add(obj)

    async def flush(self):
        self._s.flush()

    async def refresh(self, obj):
        self._s.refresh(obj)


def _fresh_user(ui_preferences=None):
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine, tables=[User.__table__, ClientExecutorDevice.__table__])
    sess = Session(engine)
    user = User(
        id="u-12345678aaaa", username="tester", password_hash="x",
        is_active=True, ui_preferences=ui_preferences,
    )
    sess.add(user)
    sess.commit()
    return sess, _Bridge(sess), user


class TestUiPreferencesMerge:
    def test_skin_update_preserves_other_keys(self):
        """G7：皮肤切换不得抹掉并存键（旧实现整对象覆写）。"""
        import json

        from app.api.skins import update_ui_preferences
        from app.schemas.skin import UiPreferencesUpdate

        sess, db, user = _fresh_user(
            ui_preferences=json.dumps({"default_execution_target": "device:abc123", "skin_id": "verdant-flat"})
        )
        result = asyncio.run(
            update_ui_preferences(UiPreferencesUpdate(skin_id="ink-paper"), user=user, db=db)
        )
        saved = json.loads(user.ui_preferences)
        assert saved["skin_id"] == "ink-paper"
        assert saved["default_execution_target"] == "device:abc123", "皮肤更新不应抹掉执行环境默认值"
        assert result.default_execution_target == "device:abc123"

    def test_exec_target_update_roundtrip(self):
        import json

        from app.api.skins import update_ui_preferences
        from app.schemas.skin import UiPreferencesUpdate
        from app.services import client_device_service as cds

        sess, db, user = _fresh_user()
        # A4.9 M2：PUT 校验设备属主——先注册真设备
        code = cds.issue_pairing_code(user.id)["code"]
        row, _token = asyncio.run(cds.register_device(db, code=code, name="d"))
        result = asyncio.run(
            update_ui_preferences(
                UiPreferencesUpdate(default_execution_target=f"device:{row.device_id}"),
                user=user, db=db,
            )
        )
        assert result.default_execution_target == f"device:{row.device_id}"
        saved = json.loads(user.ui_preferences)
        assert saved["default_execution_target"] == f"device:{row.device_id}"

    def test_exec_target_unowned_device_422(self):
        from app.api.skins import update_ui_preferences
        from app.schemas.skin import UiPreferencesUpdate
        from fastapi import HTTPException

        sess, db, user = _fresh_user()
        with pytest.raises(HTTPException) as ei:
            asyncio.run(
                update_ui_preferences(
                    UiPreferencesUpdate(default_execution_target="device:ghost-ghost"),
                    user=user, db=db,
                )
            )
        assert ei.value.status_code == 422

    def test_exec_target_empty_clears(self):
        import json

        from app.api.skins import update_ui_preferences
        from app.schemas.skin import UiPreferencesUpdate

        sess, db, user = _fresh_user(
            ui_preferences=json.dumps({"default_execution_target": "device:abc1234"})
        )
        result = asyncio.run(
            update_ui_preferences(
                UiPreferencesUpdate(default_execution_target=""), user=user, db=db
            )
        )
        saved = json.loads(user.ui_preferences)
        assert "default_execution_target" not in saved

    def test_exec_target_null_means_untouched(self):
        # A4.9 M3 钉契约：JSON null（字段缺省）= 不动该键（合并语义）
        import json

        from app.api.skins import update_ui_preferences
        from app.schemas.skin import UiPreferencesUpdate

        sess, db, user = _fresh_user(
            ui_preferences=json.dumps({"default_execution_target": "device:abc1234"})
        )
        asyncio.run(
            update_ui_preferences(UiPreferencesUpdate(skin_id="verdant-flat"), user=user, db=db)
        )
        saved = json.loads(user.ui_preferences)
        assert saved["default_execution_target"] == "device:abc1234"

    def test_bad_exec_target_422(self):
        from app.api.skins import update_ui_preferences
        from app.schemas.skin import UiPreferencesUpdate
        from fastapi import HTTPException

        sess, db, user = _fresh_user()
        with pytest.raises(HTTPException) as ei:
            asyncio.run(
                update_ui_preferences(
                    UiPreferencesUpdate(default_execution_target="cloud:x"),
                    user=user, db=db,
                )
            )
        assert ei.value.status_code == 422

    def test_unknown_skin_still_400(self):
        from app.api.skins import update_ui_preferences
        from app.schemas.skin import UiPreferencesUpdate
        from fastapi import HTTPException

        sess, db, user = _fresh_user()
        with pytest.raises(HTTPException) as ei:
            asyncio.run(
                update_ui_preferences(UiPreferencesUpdate(skin_id="nope"), user=user, db=db)
            )
        assert ei.value.status_code == 400


class TestMultiworkerGuard:
    def test_single_worker_passes(self):
        from app.services.device_session_service import guard_multiworker_client_exec

        assert guard_multiworker_client_exec({"WEB_CONCURRENCY": "1"}) is True

    def test_multi_worker_disables_client_exec(self):
        from app.core.config import get_config
        from app.services.device_session_service import guard_multiworker_client_exec

        cfg = get_config()
        prev = cfg._config.get("client_exec", {}).get("enabled", True)
        try:
            cfg._config.setdefault("client_exec", {})["enabled"] = True
            assert guard_multiworker_client_exec({"WEB_CONCURRENCY": "4"}) is False
            assert cfg._config["client_exec"]["enabled"] is False
        finally:
            cfg._config.setdefault("client_exec", {})["enabled"] = prev

    def test_wt_workers_env_alias(self):
        from app.core.config import get_config
        from app.services.device_session_service import guard_multiworker_client_exec

        cfg = get_config()
        prev = cfg._config.get("client_exec", {}).get("enabled", True)
        try:
            cfg._config.setdefault("client_exec", {})["enabled"] = True
            assert guard_multiworker_client_exec({"WT_WORKERS": "2"}) is False
        finally:
            cfg._config.setdefault("client_exec", {})["enabled"] = prev


class TestMediaType:
    def test_typed_media(self):
        from app.api.client_devices import _media_type_for

        assert _media_type_for("a.png") == "image/png"
        assert _media_type_for("dir/b.mp4") == "video/mp4"
        assert _media_type_for("c.wav") == "audio/x-wav" or _media_type_for("c.wav").startswith("audio/")
        assert _media_type_for("d.txt").startswith("text/")

    def test_unknown_falls_back(self):
        from app.api.client_devices import _media_type_for

        assert _media_type_for("x.zzznope") == "application/octet-stream"


class TestMultiworkerGuardAliases:
    def test_uvicorn_workers_alias(self):
        from app.core.config import get_config
        from app.services.device_session_service import guard_multiworker_client_exec

        cfg = get_config()
        prev = cfg._config.get("client_exec", {}).get("enabled", True)
        try:
            cfg._config.setdefault("client_exec", {})["enabled"] = True
            assert guard_multiworker_client_exec({"UVICORN_WORKERS": "3"}) is False
        finally:
            cfg._config.setdefault("client_exec", {})["enabled"] = prev

    def test_unparseable_warns_and_passes(self):
        from app.services.device_session_service import guard_multiworker_client_exec

        assert guard_multiworker_client_exec({"WEB_CONCURRENCY": "many"}) is True
