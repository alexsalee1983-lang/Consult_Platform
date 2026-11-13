"""M4 ①：项目验收与保留期（app/project/retention.py）。"""
import json
import secrets
from datetime import date, timedelta

from sqlalchemy import select

from app.cli import main as cli_main
from app.core.db import SessionLocal
from app.core.permissions import CONSULTANT
from app.main import app
from app.models import AuditLog, Project
from app.project import retention

from .conftest import create_user, logged_in_client


def test_add_months_clamps_to_month_end():
    assert retention.add_months(date(2026, 8, 31), 6) == date(2027, 2, 28)
    assert retention.add_months(date(2027, 8, 31), 6) == date(2028, 2, 29)
    assert retention.add_months(date(2026, 11, 13), 6) == date(2027, 5, 13)
    assert retention.add_months(date(2026, 11, 13), 0) == date(2026, 11, 13)
    assert retention.add_months(date(2026, 12, 1), 13) == date(2028, 1, 1)


def test_status_states():
    p = Project(code="X", service="", industry="", tier="standard")
    assert retention.status(p).state == retention.ACTIVE and retention.status(p).days_left is None
    p.accepted_on, p.deletion_due_on = date(2026, 1, 1), date(2026, 7, 1)
    assert retention.status(p, date(2026, 5, 31)).state == retention.RETAINED      # 31 天
    assert retention.status(p, date(2026, 6, 1)) == retention.Status(retention.DUE_SOON, 30)
    assert retention.status(p, date(2026, 7, 1)) == retention.Status(retention.DUE, 0)
    assert retention.status(p, date(2026, 7, 3)).days_left == -2


def _project(manager: str) -> str:
    code = f"QA-{secrets.token_hex(2).upper()}"
    assert cli_main(["create-project", "--code", code, "--tier", "standard", "--manager", manager]) == 0
    return code


def test_manager_registers_acceptance_and_retention():
    mgr = create_user((CONSULTANT,))
    member = create_user((CONSULTANT,))
    code = _project(mgr[0])
    c, m = logged_in_client(app, mgr), logged_in_client(app, member)
    try:
        assert c.put(f"/api/projects/{code}/members/{member[0]}", json={"project_role": "member"}).status_code == 200
        url = f"/api/projects/{code}/acceptance"
        r = c.get(f"/api/projects/{code}")
        assert r.json()["retention_state"] == "active" and r.json()["deletion_due_on"] is None

        today = date.today()
        assert m.post(url, json={"accepted_on": today.isoformat()}).status_code == 403            # 只有项目经理
        tomorrow = (today + timedelta(days=1)).isoformat()
        assert c.post(url, json={"accepted_on": tomorrow}).status_code == 422                       # 不能晚于今天
        assert c.post(url, json={"accepted_on": today.isoformat(), "retention_months": 25}).status_code == 422
        assert c.post(url, json={"accepted_on": today.isoformat(), "retention_months": -1}).status_code == 422

        r = c.post(url, json={"accepted_on": today.isoformat()})                                   # 默认 6 个月
        assert r.status_code == 200
        body = r.json()
        assert body["retention_months"] == 6 and body["phase_name"] == "验收"
        assert body["deletion_due_on"] == retention.add_months(today, 6).isoformat()
        assert body["retention_state"] == "retained" and body["days_to_deletion"] > 30

        # 更正：按客户制度缩短到 0 个月 → 当天到期
        r = c.post(url, json={"accepted_on": today.isoformat(), "retention_months": 0})
        assert r.json()["retention_state"] == "due" and r.json()["days_to_deletion"] == 0
        # 一个月前验收、保留 1 个月 → 30 天内到期，提醒
        r = c.post(url, json={"accepted_on": (today - timedelta(days=5)).isoformat(), "retention_months": 1})
        assert r.json()["retention_state"] == "due_soon"
        listed = {p["code"]: p for p in m.get("/api/projects").json()}
        assert listed[code]["retention_state"] == "due_soon"                                      # 成员都能看到提醒
    finally:
        c.__exit__(None, None, None)
        m.__exit__(None, None, None)

    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == "project.acceptance",
                                                 AuditLog.project_code == code).order_by(AuditLog.id)).all()
    assert [r.result for r in rows] == ["denied", "allowed", "allowed", "allowed"]
    d = json.loads(rows[1].detail)
    assert d["retention_months"] == 6 and d["corrected"] is False
    assert json.loads(rows[2].detail)["corrected"] is True


def test_retention_report_cli(capsys):
    mgr = create_user((CONSULTANT,))
    code = _project(mgr[0])
    c = logged_in_client(app, mgr)
    try:
        c.post(f"/api/projects/{code}/acceptance", json={"accepted_on": date.today().isoformat(),
                                                          "retention_months": 0})
    finally:
        c.__exit__(None, None, None)
    capsys.readouterr()
    assert cli_main(["retention-report"]) == 0
    out = capsys.readouterr().out
    line = next(x for x in out.splitlines() if x.startswith(code))
    assert "已到期" in line and "保留 0 个月" in line
