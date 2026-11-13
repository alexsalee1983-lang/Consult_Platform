"""M5 ②c：审计记录来源地址（X-Real-IP，由前端 nginx 覆盖为真实对端地址）。"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import audit
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog

from .conftest import create_user, login


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _last(action_prefix: str) -> dict:
    with SessionLocal() as db:
        row = db.scalar(select(AuditLog).where(AuditLog.action.startswith(action_prefix))
                        .order_by(AuditLog.id.desc()).limit(1))
        return json.loads(row.detail)


def test_login_audit_records_source_ip(client):
    user = create_user(roles={"consultant"})
    client.headers["X-Real-IP"] = "192.0.2.10"
    assert login(client, *user).status_code == 200
    assert _last("auth.login")["ip"] == "192.0.2.10"
    client.headers["X-Real-IP"] = "2001:db8::1"
    assert login(client, user[0], "wrong-password-xx", user[2]).status_code == 401
    assert _last("auth.login")["ip"] == "2001:db8::1"


def test_invalid_ip_header_not_written(client):
    user = create_user(roles={"consultant"})
    client.headers["X-Real-IP"] = "<script>evil</script>"
    login(client, *user)
    assert "ip" not in _last("auth.login")  # TestClient 的直连地址 "testclient" 也不是 IP


def test_no_ip_outside_requests():
    with SessionLocal() as db:
        audit.record(db, actor="cli", action="test.no_ip", result="allowed", n=1)
    assert _last("test.no_ip") == {"n": 1}
    with SessionLocal() as db:
        assert audit.verify_chain(db).ok
