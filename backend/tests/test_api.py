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


def test_projects_seeded(client):
    codes = [p["code"] for p in client.get("/api/projects").json()]
    assert codes == ["BANK-A", "GRID-B", "MFG-C"]


def test_project_detail(client):
    p = client.get("/api/projects/BANK-A").json()
    assert p["phase_name"] == "设计"
    assert len(p["deliverables"]) == 8
    assert p["open_confirmations"] == 3


def test_unknown_project_404(client):
    assert client.get("/api/projects/NOPE").status_code == 404


def test_chat_demo_reply(client):
    r = client.post("/api/chat", json={"project_code": "BANK-A", "message": "一期人力模型怎么选？"})
    assert r.status_code == 200
    assert r.json()["demo"] is True


def test_chat_blocks_confidential(client):
    r = client.post("/api/chat", json={"project_code": "BANK-A", "message": "核心交换机 10.1.20.1，密码：Abc@12345"})
    assert r.status_code == 422
    kinds = {f["kind"] for f in r.json()["detail"]["findings"]}
    assert {"ip", "credential"} <= kinds


def test_audit_has_no_raw_text_and_actor_is_session_user(client):
    client.post("/api/gateway/check", json={"text": "密码：Secret999"})
    rows = client.get("/api/audit").json()
    assert rows and all("Secret999" not in r["detail"] for r in rows)
    assert rows[0]["actor"] == "tester"


def test_actor_header_from_client_is_ignored(client):
    client.post("/api/gateway/check", json={"text": "普通问题"}, headers={"X-User": "someone-else"})
    assert client.get("/api/audit").json()[0]["actor"] == "tester"


def test_resolve_confirmation(client):
    first = client.get("/api/confirmations").json()[0]
    r = client.post(f"/api/confirmations/{first['id']}/resolve", json={"resolution": "选 A"})
    assert r.status_code == 200 and r.json()["status"] == "resolved"
