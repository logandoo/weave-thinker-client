"""遗留清偿 #4：admin.create_user username 校验（对齐注册路径 2–50）+ pair 文案区分。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_admin_create_user.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from sqlalchemy import create_engine, event, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db.database import Base, User  # noqa: E402


_ENGINES = []


def _bridge():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine, tables=[User.__table__])
    sess = Session(engine)
    sess.add(User(id="u-admin0000001", username="admin", password_hash="x", is_active=True))
    sess.commit()

    class _Bridge:
        def __init__(self, s):
            self._s = s

        async def execute(self, stmt):
            return self._s.execute(stmt)

        async def commit(self):
            self._s.commit()

        def add(self, obj):
            self._s.add(obj)

        async def refresh(self, obj):
            self._s.refresh(obj)

    _ENGINES.append(engine)
    return _Bridge(sess)


def _client(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import admin
    from app.core.deps import get_db

    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[get_db] = lambda: db

    def _user():
        return User(id="u-admin0000001", username="admin", password_hash="x", is_active=True)

    from app.core.deps import get_current_user

    app.dependency_overrides[get_current_user] = _user
    return TestClient(app)


@pytest.fixture(autouse=True)
def _dispose_engines():
    yield
    while _ENGINES:
        try:
            _ENGINES.pop().dispose()
        except Exception:
            pass


class TestCreateUserUsernameValidation:
    def test_empty_username_rejected(self):
        r = _client(_bridge()).post(
            "/api/admin/users", params={"username": "", "password": "secret123"}
        )
        assert r.status_code in (400, 422), r.text

    def test_too_short_username_rejected(self):
        r = _client(_bridge()).post(
            "/api/admin/users", params={"username": "x", "password": "secret123"}
        )
        assert r.status_code in (400, 422), r.text

    def test_too_long_username_rejected(self):
        r = _client(_bridge()).post(
            "/api/admin/users", params={"username": "u" * 51, "password": "secret123"}
        )
        assert r.status_code in (400, 422), r.text

    def test_valid_username_accepted(self):
        r = _client(_bridge()).post(
            "/api/admin/users", params={"username": "newbie", "password": "secret123"}
        )
        assert r.status_code == 200, r.text
