"""业务接口的基本行为，用测试账号（顾问；BANK-A 项目经理、MFG-C 项目成员、不在 GRID-B）。"""
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

from .conftest import login  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        r = login(c)
        assert r.status_code == 200, r.text
        c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        yield c


def test_health_reports_database_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["checks"]["database"] == "ok"


def test_me_has_roles(client):
    assert client.get("/api/auth/me").json()["roles"] == ["consultant"]


def test_only_member_projects_listed(client):
    codes = [p["code"] for p in client.get("/api/projects").json()]
    assert codes == ["BANK-A", "MFG-C"]


def test_project_detail(client):
    p = client.get("/api/projects/BANK-A").json()
    assert p["phase_name"] == "设计"
    assert len(p["deliverables"]) == 8
    assert p["open_confirmations"] == 3


def test_unknown_and_non_member_project_both_404(client):
    unknown = client.get("/api/projects/NOPE")
    other = client.get("/api/projects/GRID-B")
    assert unknown.status_code == other.status_code == 404
    assert unknown.json() == other.json()  # 不暴露项目是否存在


def test_chat_demo_reply(client):
    r = client.post("/api/chat", json={"project_code": "BANK-A", "message": "一期人力模型怎么选？"})
    assert r.status_code == 200
    assert r.json()["demo"] is True


def test_chat_blocks_confidential(client):
    r = client.post("/api/chat", json={"project_code": "BANK-A", "message": "核心交换机 10.1.20.1，密码：Abc@12345"})
    assert r.status_code == 422
    kinds = {f["kind"] for f in r.json()["detail"]["findings"]}
    assert {"ip", "credential"} <= kinds


def test_confirmations_only_from_member_projects(client):
    codes = {c["project_code"] for c in client.get("/api/confirmations").json()}
    assert codes and "GRID-B" not in codes


def test_resolve_confirmation(client):
    first = client.get("/api/confirmations").json()[0]
    r = client.post(f"/api/confirmations/{first['id']}/resolve", json={"resolution": "选 A"})
    assert r.status_code == 200 and r.json()["status"] == "resolved"


def test_members_listed(client):
    names = {m["username"]: m["project_role"] for m in client.get("/api/projects/BANK-A/members").json()}
    assert names["tester"] == "manager"
