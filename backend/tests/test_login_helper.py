"""测试基建：conftest.login() 同一用户短时间内多次登录不撞 TOTP 防重放（#505）。"""
import pytest
from fastapi.testclient import TestClient

from tests.conftest import login


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_same_user_logs_in_three_times_in_a_row(client, make_user):
    user = make_user()
    for _ in range(3):
        client.cookies.clear()
        r = login(client, *user)
        assert r.status_code == 200, r.text
