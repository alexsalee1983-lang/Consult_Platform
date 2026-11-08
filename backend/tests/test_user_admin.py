"""用户管理界面的后端：网页建账号、重置口令和动态码、首次登录强制改口令、用户目录。"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.security import check_password_policy, generate_initial_password
from app.main import app
from app.models import AuditLog, User

from .conftest import create_user, logged_in_client, login

NEW_PASSWORD = "Nn9#" + "x" * 12   # 测试用新口令，满足口令策略


@pytest.fixture()
def admin():
    c = logged_in_client(app, create_user(("sys_admin",)))
    yield c
    c.__exit__(None, None, None)


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _audits(action: str, target: str) -> list[AuditLog]:
    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)).all()
    return [a for a in rows if json.loads(a.detail or "{}").get("target") == target]


def _new_user(admin, name: str, roles=("consultant",)) -> dict:
    r = admin.post("/api/admin/users", json={"username": name, "display_name": "新同事", "roles": list(roles)})
    assert r.status_code == 201, r.text
    return r.json()


def _login_new(client, name: str, password: str, secret: str, step_offset: int = 0):
    r = login(client, name, password, secret, step_offset)
    if r.status_code == 200:
        client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return r


# ---- 建账号 ----

def test_generated_password_meets_policy():
    for _ in range(200):
        check_password_policy(generate_initial_password(), get_settings().password_min_length)


def test_create_user_returns_credentials_once_and_audits_without_secrets(admin):
    r = admin.post("/api/admin/users", json={"username": "new_a1", "display_name": "张三", "roles": ["consultant"]})
    assert r.status_code == 201
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["user"]["roles"] == ["consultant"] and body["user"]["must_change_password"] is True
    assert body["initial_password"] and body["totp_secret"] and body["totp_uri"].startswith("otpauth://")
    (a,) = _audits("user.create", "new_a1")
    assert a.result == "allowed"
    assert body["initial_password"] not in a.detail and body["totp_secret"] not in a.detail
    listed = {u["username"]: u for u in admin.get("/api/admin/users").json()}
    assert listed["new_a1"]["display_name"] == "张三"


@pytest.mark.parametrize(("payload", "status"), [
    ({"username": "new_aud", "roles": ["security_auditor"]}, 403),       # 审计员只能命令行任命
    ({"username": "new_mix", "roles": ["sys_admin", "consultant"]}, 422),  # 三权分立互斥
    ({"username": "new_unk", "roles": ["root"]}, 422),
    ({"username": "1bad"}, 422),
    ({"username": "has space"}, 422),
    ({"username": "tester"}, 409),                                        # 已存在
])
def test_create_user_rules(admin, payload, status):
    assert admin.post("/api/admin/users", json=payload).status_code == status
    with SessionLocal() as db:
        if status != 409:
            assert db.scalar(select(User).where(User.username == payload["username"])) is None


def test_create_auditor_attempt_is_audited(admin):
    admin.post("/api/admin/users", json={"username": "new_aud2", "roles": ["security_auditor"]})
    (a,) = _audits("user.create", "new_aud2")
    assert a.result == "blocked" and json.loads(a.detail)["reason"] == "grant_auditor"


# ---- 首次登录强制改口令 ----

def test_first_login_must_change_password(admin, client):
    cred = _new_user(admin, "new_b1")
    r = _login_new(client, "new_b1", cred["initial_password"], cred["totp_secret"])
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    # 改口令前只能访问 me / logout / password
    assert client.get("/api/auth/me").status_code == 200
    r = client.get("/api/projects")
    assert r.status_code == 403 and "修改初始口令" in r.json()["detail"]
    assert client.post("/api/chat", json={"project_code": "BANK-A", "message": "x"}).status_code == 403

    pw = {"old_password": cred["initial_password"]}
    assert client.post("/api/auth/password", json={**pw, "new_password": "short"}).status_code == 422
    assert client.post("/api/auth/password", json={**pw, "new_password": cred["initial_password"]}).status_code == 422
    assert client.post("/api/auth/password",
                       json={"old_password": "wrong", "new_password": NEW_PASSWORD}).status_code == 400

    r = client.post("/api/auth/password", json={**pw, "new_password": NEW_PASSWORD})
    assert r.status_code == 200 and r.json()["must_change_password"] is False
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    assert client.get("/api/projects").status_code == 200       # 新会话可用
    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == "auth.password_change",
                                                 AuditLog.actor == "new_b1").order_by(AuditLog.id)).all()
    assert [a.result for a in rows] == ["blocked", "allowed"]   # 口令策略不通过不记失败，旧口令错记一次
    assert all(NEW_PASSWORD not in (a.detail or "") for a in rows)

    # 旧口令失效，新口令可登录
    with TestClient(app) as c2:
        assert login(c2, "new_b1", cred["initial_password"], cred["totp_secret"], 1).status_code == 401
    with TestClient(app) as c3:
        assert login(c3, "new_b1", NEW_PASSWORD, cred["totp_secret"], 1).status_code == 200


def test_password_change_ends_other_sessions():
    name, password, secret = create_user(("consultant",))
    other = logged_in_client(app, (name, password, secret))
    try:
        with TestClient(app) as me:
            assert _login_new(me, name, password, secret, 1).status_code == 200
            r = me.post("/api/auth/password", json={"old_password": password, "new_password": NEW_PASSWORD})
            assert r.status_code == 200
        assert other.get("/api/auth/me").status_code == 401
    finally:
        other.__exit__(None, None, None)


def test_wrong_old_password_locks_account_and_ends_session():
    name, password, secret = create_user(("consultant",))
    c = logged_in_client(app, (name, password, secret))
    try:
        codes = [c.post("/api/auth/password", json={"old_password": f"wrong{i}", "new_password": NEW_PASSWORD})
                 .status_code for i in range(get_settings().login_max_failures)]
        assert codes[:-1] == [400] * (len(codes) - 1) and codes[-1] == 401
        assert c.get("/api/auth/me").status_code == 401
    finally:
        c.__exit__(None, None, None)
    with TestClient(app) as c2:   # 锁定期间正确口令也登录不了
        assert login(c2, name, password, secret, 1).status_code == 401


# ---- 重置口令、动态码 ----

def test_reset_password_ends_sessions_unlocks_and_forces_change(admin):
    name, password, secret = create_user(("consultant",))
    victim = logged_in_client(app, (name, password, secret))
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.username == name))
        u.failed_logins = 3
        db.commit()
    try:
        r = admin.post(f"/api/admin/users/{name}/reset-password")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        body = r.json()
        assert body["initial_password"] and body["totp_secret"] == ""
        assert body["user"]["must_change_password"] is True
        assert victim.get("/api/auth/me").status_code == 401
    finally:
        victim.__exit__(None, None, None)
    with TestClient(app) as c:
        assert login(c, name, password, secret, 1).status_code == 401
    with TestClient(app) as c:
        r = login(c, name, body["initial_password"], secret, 1)
        assert r.status_code == 200 and r.json()["must_change_password"] is True
    (a,) = _audits("user.reset_password", name)
    assert body["initial_password"] not in a.detail


def test_reset_totp_invalidates_old_secret(admin):
    name, password, secret = create_user(("consultant",))
    victim = logged_in_client(app, (name, password, secret))
    try:
        body = admin.post(f"/api/admin/users/{name}/reset-totp").json()
        assert body["totp_secret"] and body["totp_secret"] != secret and body["initial_password"] == ""
        assert body["user"]["must_change_password"] is False
        assert victim.get("/api/auth/me").status_code == 401
    finally:
        victim.__exit__(None, None, None)
    with TestClient(app) as c:
        assert login(c, name, password, secret, 1).status_code == 401
    with TestClient(app) as c:
        assert login(c, name, password, body["totp_secret"], 1).status_code == 200
    (a,) = _audits("user.reset_totp", name)
    assert body["totp_secret"] not in a.detail


def test_admin_cannot_reset_self_or_auditor(admin):
    me = admin.get("/api/auth/me").json()["username"]
    auditor, *_ = create_user(("security_auditor",))
    for action in ("reset-password", "reset-totp"):
        assert admin.post(f"/api/admin/users/{me}/{action}").status_code == 403
        assert admin.post(f"/api/admin/users/{auditor}/{action}").status_code == 403
        assert admin.post(f"/api/admin/users/nobody_x/{action}").status_code == 404


def test_cli_and_existing_accounts_not_forced_to_change(client):
    r = login(client)
    assert r.status_code == 200 and r.json()["must_change_password"] is False


# ---- 用户目录 ----

def test_lookup_returns_only_active_business_users():
    consultant, *_ = create_user(("consultant",), username="look_cons")
    create_user(("sys_admin",), username="look_admin")
    create_user(("security_auditor",), username="look_aud")
    create_user((), username="look_none")
    inactive, *_ = create_user(("consultant",), username="look_off")
    with SessionLocal() as db:
        db.scalar(select(User).where(User.username == inactive)).is_active = False
        db.commit()
    c = logged_in_client(app, create_user(("consultant",)))
    try:
        got = c.get("/api/users/lookup", params={"q": "look_"}).json()
        assert [u["username"] for u in got] == ["look_cons"]
        assert set(got[0]) == {"username", "display_name"}
        assert c.get("/api/users/lookup", params={"q": "%"}).json() == []   # % 不当通配符
        assert c.get("/api/users/lookup").status_code == 422
    finally:
        c.__exit__(None, None, None)
