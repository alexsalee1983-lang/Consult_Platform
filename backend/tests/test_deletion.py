"""M4 ②：项目资料删除与删除记录（app/project/deletion.py）。"""
import json
import secrets
from datetime import date, timedelta

from sqlalchemy import select

from app.cli import main as cli_main
from app.core.db import SessionLocal
from app.core.permissions import CONSULTANT, PROJECT_LEAD
from app.main import app
from app.models import AuditLog, Conversation, Evidence, KbUpload, Message, Project, ProjectDeletion, ProjectMember
from app.project import deletion
from app.services import jobs, storage

from .conftest import create_user, logged_in_client

NOTE = {"kind": "observation", "title": "删除测试：值班室巡查", "content": "值班台交接班记录为纸质。"}
OUTLINE = {"titles": ["现状", "差距"]}
SECTION = {"body": "删除测试正文：交接班记录为纸质 [O-01]。"}
REF = "删函-2026-07"


def _work() -> None:
    with SessionLocal() as db:
        while jobs.run_once(db, "test-worker", kinds=[deletion.JOB_KIND]):
            pass


def _project(manager: str) -> str:
    code = f"QD-{secrets.token_hex(2).upper()}"
    assert cli_main(["create-project", "--code", code, "--tier", "standard", "--manager", manager]) == 0
    return code


def _fill(c, code: str) -> None:
    """建一条观察、写 D7 一章、加一段对话，让删除有东西可删。"""
    assert c.post(f"/api/projects/{code}/notes", json=NOTE).status_code == 201
    assert c.put(f"/api/projects/{code}/deliverables/D7/outline", json=OUTLINE).status_code == 200
    assert c.put(f"/api/projects/{code}/deliverables/D7/sections/1", json=SECTION).status_code == 201
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        m = db.scalar(select(ProjectMember).where(ProjectMember.project_id == p.id))
        conv = Conversation(project_id=p.id, user_id=m.user_id, agent_code="consultant-lead", title="删除测试对话",
                            prompt_version="test")
        db.add(conv)
        db.flush()
        db.add(Message(conversation_id=conv.id, role="user", content="删除测试提问"))
        db.commit()


def test_add_working_days():
    assert deletion.add_working_days(date(2026, 11, 13), 1) == date(2026, 11, 16)    # 周五 → 下周一
    assert deletion.add_working_days(date(2026, 11, 13), 10) == date(2026, 11, 27)
    assert deletion.add_working_days(date(2026, 11, 14), 5) == date(2026, 11, 20)    # 周六收到


def test_early_deletion_flow(monkeypatch):
    monkeypatch.setattr(storage, "enabled", lambda: False)
    pm, member, lead = create_user((CONSULTANT,)), create_user((CONSULTANT,)), create_user((PROJECT_LEAD,))
    code = _project(pm[0])
    c, m, ld = logged_in_client(app, pm), logged_in_client(app, member), logged_in_client(app, lead)
    try:
        assert c.put(f"/api/projects/{code}/members/{member[0]}", json={"project_role": "member"}).status_code == 200
        _fill(c, code)
        url = f"/api/projects/{code}/deletions"
        today = date.today()
        body = {"request_ref": REF, "received_on": today.isoformat()}
        assert m.post(url, json=body).status_code == 403                                   # 只有项目经理
        assert ld.post(url, json=body).status_code == 404                                  # 负责人不是成员
        assert c.post(url, json={**body, "received_on": (today + timedelta(days=1)).isoformat()}).status_code == 422
        assert c.post(url, json={**body, "request_ref": "  "}).status_code == 422
        assert c.post(url, json={**body, "request_ref": "口令 Passw0rd!2026"}).status_code == 422   # 机密级检测
        r = c.post(url, json=body)
        assert r.status_code == 201, r.text
        d = r.json()
        assert d["kind"] == "early" and d["status"] == "pending"
        assert d["due_by"] == deletion.add_working_days(today, 10).isoformat()
        assert c.post(url, json=body).status_code == 409                                   # 已有未完成的申请
        assert [x["id"] for x in m.get(url).json()] == [d["id"]]                           # 成员能看到

        # 确认队列只给项目负责人
        assert c.get("/api/deletions").status_code == 403
        assert ld.get("/api/deletions").status_code == 200
        assert d["id"] in [x["id"] for x in ld.get("/api/deletions").json()]
        detail = ld.get(f"/api/deletions/{d['id']}").json()
        assert detail["pending_counts"]["evidences"] == 1 and detail["pending_counts"]["section_versions"] == 1
        assert detail["record"] is None

        confirm = f"/api/deletions/{d['id']}/confirm"
        assert c.post(confirm, json={"flowback": "none"}).status_code == 403              # 项目经理不能确认
        assert ld.post(confirm, json={"flowback": "maybe"}).status_code == 422
        assert ld.post(confirm, json={"flowback": "done", "flowback_refs": []}).status_code == 422
        assert ld.post(confirm, json={"flowback": "done", "flowback_refs": [99999999]}).status_code == 422
        with SessionLocal() as db:
            up = KbUpload(object_key=f"deltest/{secrets.token_hex(4)}", filename="x.md", sha256="0" * 64,
                          uploaded_by=pm[0], guard_categories=[])
            db.add(up)
            db.commit()
            up_id = up.id
        r = ld.post(confirm, json={"flowback": "done", "flowback_refs": [up_id]})
        assert r.status_code == 200 and r.json()["status"] == "confirmed", r.text
        assert ld.post(confirm, json={"flowback": "none"}).status_code == 409             # 不能再确认
        assert ld.post(f"/api/deletions/{d['id']}/cancel", json={"reason": "客户已书面撤回"}).status_code == 409

        _work()
        _work()                                                                           # 重复执行无副作用
        assert c.get(f"/api/projects/{code}").status_code == 404                          # 成员关系已移除
        assert m.get(f"/api/projects/{code}").status_code == 404
        detail = ld.get(f"/api/deletions/{d['id']}").json()
        assert detail["status"] == "done" and detail["completed_at"]
        rec = detail["record"]
        assert rec["project_code"] == code and rec["flowback"] == "done" and rec["flowback_refs"] == [up_id]
        assert rec["counts"]["evidences"] == 1 and rec["counts"]["section_versions"] == 1
        assert rec["counts"]["messages"] == 1 and rec["counts"]["members"] == 2 and rec["on_time"] is True
        assert rec["backups_purged_by"] == (date.fromisoformat(rec["completed_at"][:10]) + timedelta(days=30)).isoformat()
        assert "交付件（含已签署的最终版本）及全部章节版本" in rec["scope"]
    finally:
        for x in (c, m, ld):
            x.__exit__(None, None, None)

    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        assert p.deleted_at is not None
        assert db.scalar(select(Evidence).where(Evidence.project_id == p.id)) is None
        assert db.scalar(select(Conversation).where(Conversation.project_id == p.id)) is None
        assert len(p.deliverables) == 0
        logs = db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                                 AuditLog.action.like("project.deletion.%"))).all()
    assert {a.action for a in logs} >= {"project.deletion.request", "project.deletion.confirm", "project.deletion.done"}
    assert all(REF not in (a.detail or "") and "删除测试" not in (a.detail or "") for a in logs)   # 不记文号和内容
    done = next(json.loads(a.detail) for a in logs if a.action == "project.deletion.done")
    assert done["counts"]["evidences"] == 1 and done["flowback"] == "done"
    assert cli_main(["add-member", "--project", code, "--username", pm[0], "--role", "manager"]) == 1


def test_requester_cannot_confirm_and_lead_can_cancel(monkeypatch):
    monkeypatch.setattr(storage, "enabled", lambda: False)
    both, lead2 = create_user((CONSULTANT, PROJECT_LEAD)), create_user((PROJECT_LEAD,))
    code = _project(both[0])
    c, ld = logged_in_client(app, both), logged_in_client(app, lead2)
    try:
        d = c.post(f"/api/projects/{code}/deletions",
                   json={"request_ref": REF, "received_on": date.today().isoformat()}).json()
        assert c.post(f"/api/deletions/{d['id']}/confirm", json={"flowback": "none"}).status_code == 403  # 同一人
        cancel = f"/api/deletions/{d['id']}/cancel"
        assert ld.post(cancel, json={"reason": "撤"}).status_code == 422                 # 理由太短
        r = ld.post(cancel, json={"reason": "客户已书面撤回删除要求"})
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        assert c.get(f"/api/projects/{code}").status_code == 200                          # 项目还在
        # 撤销后可以重新发起
        assert c.post(f"/api/projects/{code}/deletions",
                      json={"request_ref": REF, "received_on": date.today().isoformat()}).status_code == 201
    finally:
        c.__exit__(None, None, None)
        ld.__exit__(None, None, None)
    with SessionLocal() as db:
        reasons = [json.loads(a.detail).get("reason") for a in db.scalars(
            select(AuditLog).where(AuditLog.action == "project.deletion.confirm", AuditLog.result == "denied"))]
    assert "same_person" in reasons


def test_expiry_scan_flowback_flag_and_storage(monkeypatch):
    pm, lead = create_user((CONSULTANT,)), create_user((PROJECT_LEAD,))
    code = _project(pm[0])
    c, ld = logged_in_client(app, pm), logged_in_client(app, lead)
    deleted: list[str] = []
    monkeypatch.setattr(storage, "enabled", lambda: True)
    monkeypatch.setattr(storage, "list_keys", lambda area, prefix: [f"{prefix}documents/a/original.pdf"]
                        if area == "project" else [])
    monkeypatch.setattr(storage, "delete", lambda area, key: deleted.append(f"{area}:{key}"))
    try:
        today = date.today()
        acc = f"/api/projects/{code}/acceptance"
        assert c.post(acc, json={"accepted_on": today.isoformat(), "retention_months": 0}).json()["retention_state"] == "due"
        with SessionLocal() as db:
            made = [x.project.code for x in deletion.scan_due(db)]
            assert code in made
            assert code not in [x.project.code for x in deletion.scan_due(db)]            # 不重复发起
        mine = c.get(f"/api/projects/{code}/deletions").json()
        assert mine[0]["kind"] == "expiry" and mine[0]["requested_by"] == "platform"
        assert mine[0]["due_by"] == today.isoformat()
        assert ld.post(f"/api/deletions/{mine[0]['id']}/cancel", json={"reason": "到期申请不能撤销"}).status_code == 409

        # 项目经理按约定更正保留期 → 待确认的到期申请自动撤销
        c.post(acc, json={"accepted_on": today.isoformat(), "retention_months": 6})
        assert c.get(f"/api/projects/{code}/deletions").json()[0]["status"] == "cancelled"
        c.post(acc, json={"accepted_on": today.isoformat(), "retention_months": 0})
        assert cli_main(["deletion-scan"]) == 0
        assert cli_main(["deletion-report"]) == 0
        pending = [x for x in c.get(f"/api/projects/{code}/deletions").json() if x["status"] == "pending"]
        assert len(pending) == 1

        # 客户不同意回流：只能选“客户不同意回流”
        assert c.put(f"/api/projects/{code}/flowback", json={"allowed": False}).json() == {"flowback_allowed": False}
        assert c.get(f"/api/projects/{code}").json()["flowback_allowed"] is False
        confirm = f"/api/deletions/{pending[0]['id']}/confirm"
        assert ld.post(confirm, json={"flowback": "none"}).status_code == 422
        assert ld.post(confirm, json={"flowback": "client_refused"}).status_code == 200
        _work()
        rec = ld.get(f"/api/deletions/{pending[0]['id']}").json()["record"]
        assert rec["flowback"] == "client_refused" and rec["kind"] == "expiry" and rec["counts"]["files"] == 1
        assert deleted == [f"project:{code}/documents/a/original.pdf"]
        assert cli_main(["deletion-report", "--all"]) == 0
    finally:
        c.__exit__(None, None, None)
        ld.__exit__(None, None, None)
    with SessionLocal() as db:
        assert db.scalar(select(ProjectDeletion).where(ProjectDeletion.status == "pending",
                                                       ProjectDeletion.project.has(code=code))) is None
