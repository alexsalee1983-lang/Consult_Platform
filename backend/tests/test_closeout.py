"""四期 A31 ⑦项目终止、⑧结项清单（app/project/closeout.py）。"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal, utcnow
from app.main import app
from app.models import Confirmation, DeliverableSection, Project
from app.project import autoflow, retention

from .conftest import create_user, logged_in_client
from .test_runs import _new_project


@pytest.fixture()
def pm():
    code = _new_project()
    user = create_user(("consultant",), {code: "manager"})
    c = logged_in_client(app, user)
    yield code, c
    c.__exit__(None, None, None)


def _seed(code: str) -> None:
    """一张未处理卡、D1 已签署、D2 第 1 章被标“需更新”。"""
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        db.add(Confirmation(project_id=p.id, kind="judgment", title="边界确认", agent="方案设计 Agent"))
        d1, d2 = sorted(p.deliverables, key=lambda d: d.id)[:2]
        d1.status = "signed"
        db.add(DeliverableSection(deliverable_id=d2.id, seq=1, title="背景", stale_reason="upstream", stale_at=utcnow(),
                                  created_by="closeout-test"))
        db.commit()


def test_closeout_checklist_lists_open_items(pm):
    code, c = pm
    _seed(code)
    r = c.get(f"/api/projects/{code}/closeout")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False
    codes = [d["code"] for d in body["unsigned"]]
    assert "D1" not in codes and "D2" in codes
    assert [x["title"] for x in body["open_cards"]] == ["边界确认"]
    assert body["stale_sections"] == [{"deliverable_code": "D2", "seq": 1, "title": "背景"}]


def test_terminate_closes_cards_stops_autoflow_and_starts_retention(pm):
    code, c = pm
    _seed(code)
    r = c.post(f"/api/projects/{code}/terminate", json={"reason": "客户预算取消"})
    assert r.status_code == 200, r.text
    today = date.today()
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        assert p.terminated_on == today and p.terminate_reason == "客户预算取消"
        assert p.deletion_due_on == retention.add_months(today, p.retention_months)
        assert retention.status(p).state != retention.ACTIVE
        cards = list(db.scalars(select(Confirmation).where(Confirmation.project_id == p.id)))
        assert cards and all(x.status == "resolved" and "项目终止" in x.resolution for x in cards)
        assert code not in autoflow.scan_runs(db)
    assert c.post(f"/api/projects/{code}/terminate", json={"reason": "再来"}).status_code == 409
    assert c.get(f"/api/projects/{code}/closeout").json()["terminated_on"] == today.isoformat()


def test_only_manager_can_terminate(pm):
    code, _ = pm
    member = create_user(("consultant",), {code: "member"})
    with logged_in_client(app, member) as m:
        assert m.post(f"/api/projects/{code}/terminate", json={"reason": "x"}).status_code == 403
        assert m.get(f"/api/projects/{code}/closeout").status_code == 200
