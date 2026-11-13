"""登录、会话、CSRF、锁定、初始账号。对应 M1 验收“未登录不能访问任何接口”。"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import sessions
from app.core.bootstrap import BootstrapError, check_settings, ensure_initial_users
from app.core.config import Settings
from app.core.db import SessionLocal
from app.core.security import PasswordPolicyError, check_password_policy
from app.main import app
from app.models import AuditLog, User

from .conftest import TEST_PASSWORD, TEST_USER, login, totp_code

# 所有业务接口：未登录一律 401。新增接口时加到这里。
PROTECTED = [
    ("GET", "/api/projects"),
    ("GET", "/api/projects/BANK-A"),
    ("GET", "/api/confirmations"),
    ("POST", "/api/confirmations/1/resolve"),
    ("POST", "/api/chat"),
    ("POST", "/api/chat/stream"),
    ("GET", "/api/admin/usage"),
    ("GET", "/api/admin/agent-grants"),
    ("GET", "/api/agents"),
    ("POST", "/api/agents/x/tools/y"),
    ("GET", "/api/projects/BANK-A/conversations"),
    ("POST", "/api/projects/BANK-A/conversations"),
    ("GET", "/api/conversations/1"),
    ("POST", "/api/conversations/1/messages"),
    ("POST", "/api/gateway/check"),
    ("GET", "/api/audit"),
    ("GET", "/api/audit/verify"),
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/projects/BANK-A/members"),
    ("GET", "/api/projects/BANK-A/evidences"),
    ("GET", "/api/projects/BANK-A/evidences/I-01"),
    ("POST", "/api/projects/BANK-A/documents"),
    ("POST", "/api/projects/BANK-A/notes"),
    ("POST", "/api/projects/BANK-A/evidences/I-01/local-only"),
    ("GET", "/api/projects/BANK-A/consents"),
    ("POST", "/api/projects/BANK-A/consents"),
    ("POST", "/api/projects/BANK-A/consents/1/revoke"),
    ("GET", "/api/projects/BANK-A/summary-cards"),
    ("GET", "/api/projects/BANK-A/summary-cards/template"),
    ("POST", "/api/projects/BANK-A/summary-cards"),
    ("POST", "/api/projects/BANK-A/summary-cards/S-01/withdraw"),
    ("GET", "/api/projects/BANK-A/deliverables/D4"),
    ("GET", "/api/projects/BANK-A/confirmations"),
    ("PUT", "/api/projects/BANK-A/deliverables/D4/outline"),
    ("GET", "/api/projects/BANK-A/deliverables/D4/sections/1"),
    ("PUT", "/api/projects/BANK-A/deliverables/D4/sections/1"),
    ("GET", "/api/projects/BANK-A/deliverables/D4/export"),
    ("POST", "/api/projects/BANK-A/deliverables/D4/reviews"),
    ("GET", "/api/projects/BANK-A/deliverables/D4/reviews"),
    ("GET", "/api/projects/BANK-A/deliverables/D4/reviews/1"),
    ("POST", "/api/projects/BANK-A/deliverables/D4/reviews/1/waivers"),
    ("GET", "/api/projects/BANK-A/deliverables/D4/signoff"),
    ("POST", "/api/projects/BANK-A/deliverables/D4/signoff/submit"),
    ("POST", "/api/projects/BANK-A/deliverables/D4/signoff/sign"),
    ("POST", "/api/projects/BANK-A/deliverables/D4/signoff/return"),
    ("GET", "/api/projects/BANK-A/deliverables/D4/sections/1/diff"),
    ("POST", "/api/projects/BANK-A/runs"),
    ("GET", "/api/projects/BANK-A/runs"),
    ("GET", "/api/projects/BANK-A/runs/1"),
    ("POST", "/api/projects/BANK-A/runs/1/cancel"),
    ("PUT", "/api/projects/BANK-A/members/x"),
    ("DELETE", "/api/projects/BANK-A/members/x"),
    ("POST", "/api/projects/BANK-A/acceptance"),
    ("GET", "/api/deletions"),
    ("GET", "/api/deletions/1"),
    ("POST", "/api/deletions/1/confirm"),
    ("POST", "/api/deletions/1/cancel"),
    ("GET", "/api/projects/BANK-A/deletions"),
    ("POST", "/api/projects/BANK-A/deletions"),
    ("PUT", "/api/projects/BANK-A/flowback"),
    ("GET", "/api/admin/roles"),
    ("GET", "/api/admin/users"),
    ("PUT", "/api/admin/users/x/roles"),
    ("POST", "/api/admin/users/x/disable"),
    ("POST", "/api/admin/users/x/enable"),
    ("POST", "/api/admin/users/x/unlock"),
    ("POST", "/api/admin/users"),
    ("POST", "/api/admin/users/x/reset-password"),
    ("POST", "/api/admin/users/x/reset-totp"),
    ("POST", "/api/auth/password"),
    ("GET", "/api/users/lookup"),
    ("GET", "/api/admin/jobs"),
    ("POST", "/api/admin/jobs/selfcheck"),
    ("POST", "/api/admin/jobs/1/retry"),
    ("POST", "/api/kb/intake"),
    ("GET", "/api/kb/intake"),
    ("GET", "/api/kb/intake/1"),
    ("GET", "/api/kb/intake/1/deid"),
    ("POST", "/api/kb/intake/1/deid/rerun"),
    ("POST", "/api/kb/intake/1/deid/decision"),
    ("GET", "/api/kb/items"),
    ("POST", "/api/kb/items"),
    ("GET", "/api/kb/items/x"),
    ("POST", "/api/kb/items/x/versions"),
    ("POST", "/api/kb/items/x/retire"),
    ("POST", "/api/kb/items/x/reconfirm"),
    ("GET", "/api/kb/versions/1"),
    ("PATCH", "/api/kb/versions/1"),
    ("POST", "/api/kb/versions/1/submit"),
    ("POST", "/api/kb/versions/1/review"),
    ("GET", "/api/kb/review-queue"),
    ("GET", "/api/kb/review-due"),
    ("GET", "/api/kb/search"),
    ("GET", "/api/kb/basis"),
    ("GET", "/api/kb/meta"),
    ("GET", "/api/kb/versions"),
    ("GET", "/api/kb/stats"),
]


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _login_ok(client, user):
    r = login(client, *user)
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_protected_endpoints_require_login(client, method, path):
    assert client.request(method, path, json={}).status_code == 401


def test_every_api_route_is_protected_or_public(client):
    """防止新增接口漏掉：除白名单外，所有 /api 路由都必须出现在 PROTECTED 里。"""
    from app.core.auth import PUBLIC_PATHS

    listed = {p for _, p in PROTECTED}
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api") and path not in PUBLIC_PATHS:
            concrete = path.replace("{code}", "BANK-A").replace("{cid}", "1").replace("{username}", "x") \
                .replace("{agent}", "x").replace("{tool}", "y").replace("{job_id}", "1").replace("{upload_id}", "1").replace("{doc_id}", "x").replace("{version_id}", "1").replace("{evidence}", "I-01").replace("{dcode}", "D4").replace("{seq}", "1") \
                .replace("{rid}", "1").replace("{deletion_id}", "1") \
                .replace("{consent_id}", "1").replace("{card}", "S-01")
            assert concrete in listed, f"{path} 没有列入 PROTECTED，请补测试"


def test_public_endpoints(client):
    assert client.get("/api/health").status_code == 200
    assert client.post("/api/auth/login", json={"username": "x", "password": "y", "totp": "000000"}).status_code == 401


def test_login_sets_hardened_cookie(client, make_user):
    r = login(client, *make_user())
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert f"{sessions.COOKIE_NAME}=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie or "SameSite=Strict" in cookie
    assert r.json()["csrf_token"]
    assert client.get("/api/auth/me").status_code == 200


def test_wrong_password_and_wrong_totp_rejected(client, make_user):
    name, pw, secret = make_user()
    assert client.post("/api/auth/login", json={"username": name, "password": pw + "x",
                                                "totp": totp_code(secret)}).status_code == 401
    assert client.post("/api/auth/login", json={"username": name, "password": pw, "totp": "123456"}).status_code == 401
    assert client.get("/api/projects").status_code == 401


def test_totp_code_cannot_be_reused(client, make_user):
    user = make_user()
    code = totp_code(user[2])
    body = {"username": user[0], "password": user[1], "totp": code}
    assert client.post("/api/auth/login", json=body).status_code == 200
    client.cookies.clear()
    assert client.post("/api/auth/login", json=body).status_code == 401


def test_lockout_after_repeated_failures(client, make_user):
    name, pw, secret = make_user()
    for _ in range(5):
        client.post("/api/auth/login", json={"username": name, "password": "Wrong-pass-1", "totp": "000000"})
    r = login(client, name, pw, secret)
    assert r.status_code == 401
    assert "锁定" in r.json()["detail"]


def test_session_id_rotates_on_login(client, make_user):
    user = make_user()
    _login_ok(client, user)
    old = client.cookies.get(sessions.COOKIE_NAME)
    r = login(client, *user, step_offset=1)
    assert r.status_code == 200
    new = client.cookies.get(sessions.COOKIE_NAME)
    assert new and new != old
    assert sessions.load(old) is None  # 旧会话已作废


def test_unsafe_requests_need_csrf_token(client, make_user):
    csrf = _login_ok(client, make_user())
    assert client.post("/api/auth/logout").status_code == 403
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": "forged"}).status_code == 403
    assert client.get("/api/auth/me").status_code == 200  # 被拒绝的请求没有生效
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204


def test_me_returns_csrf_for_page_reload(client, make_user):
    name, *_ = user = make_user()
    csrf = _login_ok(client, user)
    me = client.get("/api/auth/me").json()
    assert me["username"] == name and me["csrf_token"] == csrf


def test_logout_invalidates_session(client, make_user):
    csrf = _login_ok(client, make_user())
    token = client.cookies.get(sessions.COOKIE_NAME)
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert sessions.load(token) is None
    client.cookies.set(sessions.COOKIE_NAME, token)  # 拿旧 Cookie 重放也不行
    assert client.get("/api/projects").status_code == 401


def test_idle_timeout(client, make_user, monkeypatch):
    _login_ok(client, make_user())
    t0 = sessions.now()
    monkeypatch.setattr(sessions, "now", lambda: t0 + 29 * 60)
    assert client.get("/api/auth/me").status_code == 200         # 29 分钟内有操作，续期
    monkeypatch.setattr(sessions, "now", lambda: t0 + 29 * 60 + 31 * 60)
    assert client.get("/api/auth/me").status_code == 401         # 之后 31 分钟无操作，超时


def test_absolute_lifetime(client, make_user, monkeypatch):
    _login_ok(client, make_user())
    t0 = sessions.now()
    for minutes in range(20, 12 * 60, 20):  # 一直有操作
        monkeypatch.setattr(sessions, "now", lambda m=minutes: t0 + m * 60)
        assert client.get("/api/auth/me").status_code == 200
    monkeypatch.setattr(sessions, "now", lambda: t0 + 12 * 3600 + 60)
    assert client.get("/api/projects").status_code == 401


def test_disabled_user_loses_access(client, make_user):
    name, *_ = user = make_user()
    _login_ok(client, user)
    with SessionLocal() as db:
        db.scalar(select(User).where(User.username == name)).is_active = False
        db.commit()
    assert client.get("/api/projects").status_code == 401


def test_audit_never_contains_passwords(client, make_user):
    name, pw, secret = make_user()
    client.post("/api/auth/login", json={"username": name, "password": pw + "x", "totp": "000000"})
    client.post("/api/auth/login", json={"username": pw, "password": pw, "totp": "000000"})  # 口令误填进用户名
    login(client, name, pw, secret)
    with SessionLocal() as db:
        details = [(a.actor, a.detail) for a in db.scalars(select(AuditLog).where(AuditLog.action == "auth.login"))]
    assert details and all(pw not in actor and pw not in detail for actor, detail in details)


def test_password_policy():
    with pytest.raises(PasswordPolicyError):
        check_password_policy("short1A!", 12)
    with pytest.raises(PasswordPolicyError):
        check_password_policy("alllowercaseletters", 12)
    check_password_policy("Long-enough-pass1", 12)


# ---- 启动检查与初始账号 ----

def _settings(**kw) -> Settings:
    base = {"app_env": "prod", "session_backend": "redis", "cookie_secure": True,
            "test_account_username": "", "test_account_password": "", "test_account_totp_secret": "",
            "bootstrap_admin_username": "", "bootstrap_admin_password": "", "bootstrap_admin_totp_secret": "",
            "minio_endpoint": "minio:9000", "minio_access_key": "k", "minio_secret_key": "x" * 24}
    return Settings(_env_file=None, **{**base, **kw})


def test_prod_refuses_test_account():
    with pytest.raises(BootstrapError):
        check_settings(_settings(test_account_username="t", test_account_password="x"))


def test_prod_refuses_memory_sessions_and_insecure_cookie():
    with pytest.raises(BootstrapError):
        check_settings(_settings(session_backend="memory"))
    with pytest.raises(BootstrapError):
        check_settings(_settings(cookie_secure=False))
    check_settings(_settings())


def test_no_default_accounts_without_config(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.migrate import upgrade_to_head

    eng = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    upgrade_to_head(eng)
    with sessionmaker(bind=eng)() as db:
        ensure_initial_users(db, _settings(app_env="dev"))
        assert db.scalar(select(User.id)) is None
    eng.dispose()


def test_bootstrap_admin_created_once(tmp_path):
    from sqlalchemy import create_engine, func
    from sqlalchemy.orm import sessionmaker

    from app.core.migrate import upgrade_to_head
    from app.core.security import new_totp_secret

    eng = create_engine(f"sqlite:///{tmp_path / 'boot.db'}")
    upgrade_to_head(eng)
    s = _settings(bootstrap_admin_username="admin1", bootstrap_admin_password="Boot-strap-pass1",
                  bootstrap_admin_totp_secret=new_totp_secret())
    with sessionmaker(bind=eng)() as db:
        ensure_initial_users(db, s)
        ensure_initial_users(db, s)
        assert db.scalar(select(func.count(User.id))) == 1
        # 已有用户后，.env 里删掉口令也不报错
        ensure_initial_users(db, _settings(bootstrap_admin_username="admin1"))
    eng.dispose()


def test_bootstrap_admin_rejects_weak_password(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.migrate import upgrade_to_head
    from app.core.security import new_totp_secret

    eng = create_engine(f"sqlite:///{tmp_path / 'weak.db'}")
    upgrade_to_head(eng)
    s = _settings(bootstrap_admin_username="admin1", bootstrap_admin_password="admin",
                  bootstrap_admin_totp_secret=new_totp_secret())
    with sessionmaker(bind=eng)() as db, pytest.raises(BootstrapError):
        ensure_initial_users(db, s)
    eng.dispose()


def test_test_account_exists_in_test_env():
    with SessionLocal() as db:
        assert db.scalar(select(User).where(User.username == TEST_USER)) is not None
    assert TEST_PASSWORD  # 由 conftest 随机生成
