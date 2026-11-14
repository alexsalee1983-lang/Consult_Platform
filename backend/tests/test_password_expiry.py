"""口令有效期与长期未登录提示（M5 ②d，等保自查 8.1.4.1 a、8.1.4.2 c）。"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal, utcnow
from app.main import app
from app.models import User

from .conftest import create_user, logged_in_client, login

NEW_PASSWORD = "Nn9#" + "y" * 12


def _age(username: str, *, password_days: int | None = None, login_days: int | None = None,
         created_days: int | None = None) -> None:
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.username == username))
        if password_days is not None:
            u.password_changed_at = utcnow() - timedelta(days=password_days)
        if login_days is not None:
            u.last_login_at = utcnow() - timedelta(days=login_days)
        if created_days is not None:
            u.created_at = utcnow() - timedelta(days=created_days)
        db.commit()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_new_user_password_not_expired(client):
    name, pw, secret = create_user(("consultant",))
    r = login(client, name, pw, secret)
    assert r.status_code == 200
    assert r.json()["must_change_password"] is False and r.json()["password_expired"] is False


def test_expired_password_only_allows_change(client):
    name, pw, secret = create_user(("consultant",))
    _age(name, password_days=get_settings().password_max_age_days + 1)
    r = login(client, name, pw, secret)
    assert r.status_code == 200
    assert r.json()["must_change_password"] is True and r.json()["password_expired"] is True
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    assert client.get("/api/auth/me").status_code == 200
    r = client.get("/api/projects")
    assert r.status_code == 403 and "有效期" in r.json()["detail"]

    r = client.post("/api/auth/password", json={"old_password": pw, "new_password": NEW_PASSWORD})
    assert r.status_code == 200
    assert r.json()["must_change_password"] is False and r.json()["password_expired"] is False
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    assert client.get("/api/projects").status_code == 200


def test_within_max_age_not_expired(client):
    name, pw, secret = create_user(("consultant",))
    _age(name, password_days=get_settings().password_max_age_days - 1)
    r = login(client, name, pw, secret)
    assert r.json()["must_change_password"] is False
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    assert client.get("/api/projects").status_code == 200


def test_max_age_zero_disables(client, monkeypatch):
    name, pw, secret = create_user(("consultant",))
    _age(name, password_days=1000)
    monkeypatch.setattr(get_settings(), "password_max_age_days", 0)
    r = login(client, name, pw, secret)
    assert r.json()["must_change_password"] is False


def test_admin_reset_counts_from_reset(client):
    """管理员重置口令时显示“待改初始口令”而不是“已过期”。"""
    admin = logged_in_client(app, create_user(("sys_admin",)))
    try:
        name, pw, secret = create_user(("consultant",))
        _age(name, password_days=500)
        assert admin.post(f"/api/admin/users/{name}/reset-password").status_code == 200
        with SessionLocal() as db:
            u = db.scalar(select(User).where(User.username == name))
            at = u.password_changed_at if u.password_changed_at.tzinfo else \
                u.password_changed_at.replace(tzinfo=utcnow().tzinfo)
            assert utcnow() - at < timedelta(minutes=5)
    finally:
        admin.__exit__(None, None, None)


def test_user_list_flags_expired_and_inactive():
    admin = logged_in_client(app, create_user(("sys_admin",)))
    try:
        days = get_settings().inactive_account_days
        stale, _, _ = create_user(("consultant",))
        _age(stale, login_days=days + 1, password_days=get_settings().password_max_age_days + 1)
        never, _, _ = create_user(("consultant",))
        _age(never, created_days=days + 1)            # 从没登录过：按建号时间算
        fresh, _, _ = create_user(("consultant",))
        _age(fresh, login_days=1)
        users = {u["username"]: u for u in admin.get("/api/admin/users").json()}
        assert users[stale]["inactive"] is True and users[stale]["password_expired"] is True
        assert users[never]["inactive"] is True
        assert users[fresh]["inactive"] is False and users[fresh]["password_expired"] is False
        # 已停用的账号不再提示
        assert admin.post(f"/api/admin/users/{stale}/disable").status_code == 200
        users = {u["username"]: u for u in admin.get("/api/admin/users").json()}
        assert users[stale]["inactive"] is False
    finally:
        admin.__exit__(None, None, None)
