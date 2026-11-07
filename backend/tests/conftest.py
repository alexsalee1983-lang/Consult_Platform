import os
import secrets
import sys
import time
from pathlib import Path

import pyotp
import pytest

# 测试用 SQLite 和内存会话，不依赖 Postgres / Redis；不配置模型地址，走演示回复。
_db_file = Path(__file__).parent / "test.db"
if _db_file.exists():
    _db_file.unlink()
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file}"
os.environ["LLM_BASE_URL"] = ""
os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"
os.environ["MINIO_ENDPOINT"] = "127.0.0.1:1"
os.environ["SESSION_BACKEND"] = "memory"
os.environ["COOKIE_SECURE"] = "false"  # TestClient 走 http://testserver

# 测试账号每次随机生成，代码里没有固定口令
TEST_USER = "tester"
TEST_PASSWORD = "Aa1!" + secrets.token_urlsafe(16)
TEST_TOTP_SECRET = pyotp.random_base32()
os.environ["TEST_ACCOUNT_USERNAME"] = TEST_USER
os.environ["TEST_ACCOUNT_PASSWORD"] = TEST_PASSWORD
os.environ["TEST_ACCOUNT_TOTP_SECRET"] = TEST_TOTP_SECRET
for k in ("BOOTSTRAP_ADMIN_USERNAME", "BOOTSTRAP_ADMIN_PASSWORD", "BOOTSTRAP_ADMIN_TOTP_SECRET"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def totp_code(secret: str, step_offset: int = 0) -> str:
    """step_offset=1 取下一个 30 秒时间步的动态码，用于同一用户在一个测试里第二次登录（防重放会拒绝同一步）。"""
    return pyotp.TOTP(secret).at(time.time() + 30 * step_offset)


def login(client, username: str = TEST_USER, password: str = TEST_PASSWORD, secret: str = TEST_TOTP_SECRET,
          step_offset: int = 0):
    return client.post("/api/auth/login", json={"username": username, "password": password,
                                                "totp": totp_code(secret, step_offset)})


def create_user(roles=(), projects=None, username: str | None = None):
    """在库里直接建一个用户，返回 (用户名, 口令, TOTP 密钥)。

    roles：系统角色代码；projects：{项目代号: "manager" / "member"}。
    """
    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.core.permissions import set_roles
    from app.core.security import hash_password
    from app.models import Project, ProjectMember, User

    username = username or "u_" + secrets.token_hex(4)
    password = "Aa1!" + secrets.token_urlsafe(16)
    secret = pyotp.random_base32()
    with SessionLocal() as db:
        user = User(username=username, display_name=username, password_hash=hash_password(password),
                    totp_secret=secret)
        db.add(user)
        db.commit()
        if roles:
            set_roles(db, user, roles, granted_by="test")
        for code, role in (projects or {}).items():
            p = db.scalar(select(Project).where(Project.code == code))
            db.add(ProjectMember(project_id=p.id, user_id=user.id, project_role=role, added_by="test"))
        db.commit()
    return username, password, secret


@pytest.fixture()
def make_user():
    return create_user


def logged_in_client(app, user):
    """登录后的 TestClient（已带 CSRF 头）。调用方负责 close。"""
    from fastapi.testclient import TestClient

    c = TestClient(app)
    c.__enter__()
    r = login(c, *user)
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c
