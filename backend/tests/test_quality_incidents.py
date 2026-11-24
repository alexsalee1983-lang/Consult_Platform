"""四期 F3：AI 质量事件台账（4A 架构设计 6.3）。

登记、分级、八步处置、关联交付件 / 知识条目 / 运行记录；只记长度、摘要哈希、类别，不记原文；
两张表只追加（库级触发器拒绝改、删）；看板（F2）出计数。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError

from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, QualityIncident, QualityIncidentEvent
from app.services import quality_incidents as qi

from .conftest import create_user, logged_in_client

SECRET_TEXT = "F3 原文标记：示范银行核心系统第三章引用了已废止条款"


@pytest.fixture()
def lead():
    c = logged_in_client(app, create_user(("project_lead",)))
    yield c
    c.close()


@pytest.fixture()
def manager():
    c = logged_in_client(app, create_user(("consultant",), {"BANK-A": "manager"}))
    yield c
    c.close()


@pytest.fixture()
def outsider():
    c = logged_in_client(app, create_user(("consultant",), {"MFG-C": "member"}))
    yield c
    c.close()


def _register(c, **kw):
    body = {"category": "repealed_basis", "project": "BANK-A", "ref_kind": "deliverable", "ref_id": "D7",
            "description": SECRET_TEXT, **kw}
    return c.post("/api/quality-incidents", json=body)


def test_register_stores_only_length_hash_and_category(manager):
    r = _register(manager)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "registered" and body["level"] is None
    assert body["description_len"] == len(SECRET_TEXT)
    assert body["description_sha"] == hashlib.sha256(SECRET_TEXT.encode()).hexdigest()[:16]
    assert SECRET_TEXT not in r.text
    with SessionLocal() as db:
        row = db.get(QualityIncident, body["id"])
        assert row.project_code == "BANK-A" and row.category == "repealed_basis"
        assert row.ref_kind == "deliverable" and row.ref_id == "D7"
        for col in QualityIncident.__table__.columns:
            assert SECRET_TEXT not in str(getattr(row, col.name))
        logs = db.scalars(select(AuditLog).where(AuditLog.action == "quality_incident.register")).all()
        assert logs and all(SECRET_TEXT not in (a.detail or "") for a in logs)


def test_register_validation_and_membership(manager, outsider):
    assert _register(manager, category="bogus").status_code == 422
    assert _register(manager, ref_kind="bogus").status_code == 422
    assert _register(outsider).status_code == 404          # 非 BANK-A 成员：项目不存在
    assert _register(outsider, project="", ref_kind="kb_item", ref_id="KB-0001").status_code == 201


def test_grade_contain_and_close_flow(manager, lead):
    iid = _register(manager).json()["id"]
    # 分级：项目经理可以，非负责人看不到全表
    r = manager.post(f"/api/quality-incidents/{iid}/events", json={"step": "grade", "level": "P1"})
    assert r.status_code == 201, r.text
    assert r.json()["level"] == "P1" and r.json()["contain_due"]
    assert manager.post(f"/api/quality-incidents/{iid}/events", json={"step": "grade"}).status_code == 422
    assert manager.post(f"/api/quality-incidents/{iid}/events",
                        json={"step": "contain", "action": "bogus"}).status_code == 422
    r = lead.post(f"/api/quality-incidents/{iid}/events",
                  json={"step": "contain", "action": "freeze_export", "note": SECRET_TEXT})
    assert r.status_code == 201 and r.json()["status"] == "contain" and r.json()["contained_at"]
    assert lead.post(f"/api/quality-incidents/{iid}/events",
                     json={"step": "root_cause", "root_cause": "bogus"}).status_code == 422
    for step in ({"step": "assess"}, {"step": "correct"}, {"step": "root_cause", "root_cause": "knowledge"},
                 {"step": "prevent"}, {"step": "review"}):
        assert lead.post(f"/api/quality-incidents/{iid}/events", json=step).status_code == 201
    detail = lead.get(f"/api/quality-incidents/{iid}").json()
    assert detail["status"] == "closed" and detail["root_cause"] == "knowledge"
    assert [e["step"] for e in detail["events"]] == ["register", "grade", "contain", "assess", "correct",
                                                    "root_cause", "prevent", "review"]
    assert SECRET_TEXT not in str(detail)
    assert lead.post(f"/api/quality-incidents/{iid}/events", json={"step": "assess"}).status_code == 409


def test_permissions(manager, outsider, lead):
    iid = _register(manager).json()["id"]
    assert outsider.get("/api/quality-incidents").status_code == 403
    assert manager.get("/api/quality-incidents").status_code == 403
    assert lead.get("/api/quality-incidents").status_code == 200
    assert outsider.get(f"/api/quality-incidents/{iid}").status_code == 404
    assert outsider.post(f"/api/quality-incidents/{iid}/events", json={"step": "assess"}).status_code == 404
    assert manager.get(f"/api/quality-incidents/{iid}").status_code == 200      # 登记人 / 本项目经理
    assert lead.get("/api/quality-incidents/999999").status_code == 404


def test_list_filters_and_overdue(lead, manager):
    iid = _register(manager).json()["id"]
    lead.post(f"/api/quality-incidents/{iid}/events", json={"step": "grade", "level": "P1"})
    with SessionLocal() as db:
        assert db.scalars(select(QualityIncidentEvent).where(QualityIncidentEvent.incident_id == iid)).all()
    r = lead.get("/api/quality-incidents", params={"status": "open"})
    assert r.status_code == 200 and any(i["id"] == iid for i in r.json()["items"])
    now = datetime.now(timezone.utc)
    assert qi.contain_due("P1", now) == now + timedelta(hours=4)
    assert qi.contain_due("P3", now) is None
    fri = datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc)          # 周五 → 下周一同一时刻
    assert qi.contain_due("P2", fri) == datetime(2026, 11, 30, 15, 0, tzinfo=timezone.utc)
    summary = lead.get("/api/indicators").json()["quality_incidents"]
    assert summary["open"] >= 1 and set(summary["by_level"]) == {"P1", "P2", "P3", "ungraded"}
    assert "overdue" in summary


def test_tables_are_append_only(manager):
    iid = _register(manager).json()["id"]
    with SessionLocal() as db:
        with pytest.raises(DatabaseError):
            db.execute(text("UPDATE quality_incidents SET category = 'x' WHERE id = :i"), {"i": iid})
            db.commit()
        db.rollback()
        with pytest.raises(DatabaseError):
            db.execute(text("DELETE FROM quality_incident_events WHERE incident_id = :i"), {"i": iid})
            db.commit()
        db.rollback()
