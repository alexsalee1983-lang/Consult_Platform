"""422 参数校验失败时不回显请求原文（口令、客户原文）。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

from .conftest import create_user, logged_in_client

SECRET = "Zq9#NotEchoed-7f3a"          # 测试用值，不是任何真实口令
TEXT = "某某城商行核心系统" * 2300      # 超过提问长度上限 20000 字


@pytest.fixture(scope="module")
def client():
    c = logged_in_client(app, create_user(("consultant",), {"BANK-A": "member"}))
    yield c
    c.__exit__(None, None, None)


def _assert_clean(r, *values):
    assert r.status_code == 422
    for err in r.json()["detail"]:
        assert set(err) <= {"loc", "msg", "type"}
    for v in values:
        assert v not in r.text


def test_wrong_field_name_does_not_echo_password(client):
    r = client.post("/api/auth/password", json={"current_password": SECRET, "new_password": SECRET})
    _assert_clean(r, SECRET)
    assert any(e["loc"][-1] == "old_password" for e in r.json()["detail"])   # 仍能看出哪个字段有问题


def test_too_long_message_does_not_echo_text(client):
    r = client.post("/api/chat", json={"project_code": "BANK-A", "message": TEXT})
    _assert_clean(r, "某某城商行")


def test_login_validation_does_not_echo_password():
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "tester", "password": SECRET, "totp": "12345678"})
    _assert_clean(r, SECRET, "12345678")


def test_invalid_json_does_not_echo_body(client):
    r = client.post("/api/auth/password", content=f'{{"old_password": "{SECRET}", ', headers={"Content-Type": "application/json"})
    _assert_clean(r, SECRET)
