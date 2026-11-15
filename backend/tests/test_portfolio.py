"""项目总览（负责人 11-15 定方案 A）：项目负责人不是成员也能看元数据，但仍进不了项目。"""
import json
import secrets

from app.core.db import SessionLocal
from app.main import app
from app.models import Deliverable, Project

from .conftest import create_user, logged_in_client

CONTENT_KEYS = {"title", "body", "content", "evidences", "confirmations", "conversations", "request_ref", "members"}


def _project() -> str:
    code = f"PF-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier="standard")
        p.deliverables = [Deliverable(code="D1", title="启动包（不应出现在总览）", status="signed"),
                          Deliverable(code="D4", title="现状评估报告", status="drafting")]
        db.add(p)
        db.commit()
    return code


def test_lead_sees_metadata_of_all_projects_but_not_content():
    code = _project()
    mgr = create_user(("consultant",), {code: "manager"})
    lead = create_user(("consultant", "project_lead", "knowledge_expert", "knowledge_admin"), {})
    with logged_in_client(app, lead) as c:
        r = c.get("/api/portfolio")
        assert r.status_code == 200
        item = next(i for i in r.json() if i["code"] == code)
        assert item["deliverables"] == {"signed": 1, "drafting": 1} and item["member_count"] == 1
        assert item["managers"] and item["is_member"] is False and item["retention_state"] == "active"
        assert not CONTENT_KEYS & set(item) and "不应出现在总览" not in json.dumps(r.json(), ensure_ascii=False)
        assert c.get(f"/api/projects/{code}").status_code == 404          # 不是成员仍然进不去
        assert c.get(f"/api/projects/{code}/evidences").status_code == 404
    with logged_in_client(app, mgr) as c:
        assert c.get("/api/portfolio").status_code == 403                  # 项目经理不是负责人
