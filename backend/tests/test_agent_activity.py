"""四期 A29：Agent 运行情况按角色可见、统一收件箱（《平台规划刷新设计》2.2、3.2、4.1、第十一节第 7 条）。

- 知识侧运行记录：知识管理员可看可重跑，咨询业务负责人只读，知识专家、顾问 403。
- 项目侧：项目经理可重跑 / 暂停，项目成员只读（发起、取消 403），非成员 404；
  咨询业务负责人跨项目只看运行状态元数据（步骤、失败类型），不看内容。
- 我的任务：别人的卡隐藏；卡片统一格式；AI 超过预计 2 倍推“交付链延迟”卡给项目经理。
"""
from __future__ import annotations

import secrets
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal, utcnow
from app.core.permissions import CONSULTANT
from app.main import app
from app.models import AgentRun, Job, Project, User
from app.products import config
from app.project import configs

from .conftest import create_user, logged_in_client


@pytest.fixture(scope="module")
def proj():
    pm = create_user(roles=(CONSULTANT,))
    code = f"BANK-AA{secrets.token_hex(2).upper()}"
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == pm[0]))
        configs.create(db, actor=pm[0], code=code, tier="standard", manager=user, product="SOC")
    member = create_user((CONSULTANT,), {code: "member"})
    return code, pm, member


def _active_run(code: str, minutes_ago: int, status: str = "running", error: str = "") -> int:
    chain = config.product("SOC").chain
    k = next(i for i, s in enumerate(chain) if s.output)
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        for r in db.scalars(select(AgentRun).where(AgentRun.project_id == p.id)):
            r.status = "done"
        at = utcnow() - timedelta(minutes=minutes_ago)
        run = AgentRun(project_id=p.id, status=status, step_index=k, waiting_cards=[], card_ids=[], created_by="t",
                       created_at=at, updated_at=at, error=error)
        db.add(run)
        db.commit()
        return run.id


def _kb_job(status: str = "failed", kind: str = "kb.parse") -> int:
    with SessionLocal() as db:
        j = Job(kind=kind, payload='{"upload_id": 1}', status=status, last_error="ValueError：解析失败")
        db.add(j)
        db.commit()
        return j.id


def test_knowledge_runs_visible_by_role():
    jid = _kb_job()
    admin = logged_in_client(app, create_user(("knowledge_admin",)))
    lead = logged_in_client(app, create_user(("project_lead",)))
    expert = logged_in_client(app, create_user(("knowledge_expert",)))
    consultant = logged_in_client(app, create_user((CONSULTANT,)))
    try:
        r = admin.get("/api/agent-activity/knowledge")
        assert r.status_code == 200 and r.json()["can_manage"] is True
        row = next(x for x in r.json()["jobs"] if x["id"] == jid)
        assert row["status"] == "failed" and row["failure"] == "ValueError"
        assert "payload" not in row and "upload_id" not in str(r.json())          # 不给载荷
        r = lead.get("/api/agent-activity/knowledge")
        assert r.status_code == 200 and r.json()["can_manage"] is False           # 只读
        assert expert.get("/api/agent-activity/knowledge").status_code == 403     # 知识专家没有这个按键
        assert consultant.get("/api/agent-activity/knowledge").status_code == 403
        # 重跑：只有知识管理员；只能重跑知识侧失败的任务
        assert lead.post(f"/api/agent-activity/knowledge/jobs/{jid}/retry").status_code == 403
        assert expert.post(f"/api/agent-activity/knowledge/jobs/{jid}/retry").status_code == 403
        r = admin.post(f"/api/agent-activity/knowledge/jobs/{jid}/retry")
        assert r.status_code == 200 and r.json()["status"] == "queued"
        assert admin.post(f"/api/agent-activity/knowledge/jobs/{jid}/retry").status_code == 409   # 已在排队
        other = _kb_job(kind="project.parse")
        assert admin.post(f"/api/agent-activity/knowledge/jobs/{other}/retry").status_code == 404
    finally:
        for c in (admin, lead, expert, consultant):
            c.close()


def test_lead_sees_cross_project_run_metadata_only(proj):
    code, _, _ = proj
    _active_run(code, 0, status="failed", error="ModelUnavailable：模型暂不可用")
    lead = logged_in_client(app, create_user(("project_lead",)))
    consultant = logged_in_client(app, create_user((CONSULTANT,)))
    try:
        r = lead.get("/api/agent-activity/projects")
        assert r.status_code == 200
        row = next(x for x in r.json() if x["project"] == code)
        assert row["status"] == "failed" and row["failure"] == "ModelUnavailable" and row["step"]
        assert set(row) == {"project", "status", "step", "step_name", "failure", "updated_at"}
        assert consultant.get("/api/agent-activity/projects").status_code == 403
        # 不是成员仍进不了项目
        assert lead.get(f"/api/projects/{code}/panorama").status_code == 404
        assert lead.get(f"/api/projects/{code}/runs").status_code == 404
    finally:
        lead.close()
        consultant.close()


def test_member_is_read_only_manager_can_rerun_and_pause(proj):
    code, pm, member = proj
    rid = _active_run(code, 0, status="waiting")
    with logged_in_client(app, member) as c:
        assert c.get(f"/api/projects/{code}/runs").status_code == 200                # 成员可看
        assert c.get(f"/api/projects/{code}/panorama?runs=1").json()["can_manage_runs"] is False
        assert c.post(f"/api/projects/{code}/runs").status_code == 403               # 成员不能发起 / 重跑
        assert c.post(f"/api/projects/{code}/runs/{rid}/cancel").status_code == 403  # 成员不能暂停
    with logged_in_client(app, pm) as c:
        assert c.get(f"/api/projects/{code}/panorama?runs=1").json()["can_manage_runs"] is True
        assert c.post(f"/api/projects/{code}/runs/{rid}/cancel").status_code == 200


def test_delay_card_goes_to_manager_only(proj):
    code, pm, member = proj
    _active_run(code, 24 * 60)
    with logged_in_client(app, pm) as c:
        body = c.get("/api/my-tasks").json()
    delay = [t for t in body["tasks"] if t["kind"] == "run_delay" and t["project_code"] == code]
    assert len(delay) == 1 and delay[0]["link"] == f"/project/{code}"
    with logged_in_client(app, member) as c:
        assert not [t for t in c.get("/api/my-tasks").json()["tasks"] if t["kind"] == "run_delay"]
    _active_run(code, 0)
    with logged_in_client(app, pm) as c:
        assert not [t for t in c.get("/api/my-tasks").json()["tasks"]
                    if t["kind"] == "run_delay" and t["project_code"] == code]


def test_inbox_cards_have_unified_format(proj):
    code, pm, _ = proj
    with logged_in_client(app, pm) as c:
        body = c.get("/api/my-tasks").json()
    assert {"pending", "minutes", "auto_done"} <= set(body)
    assert body["pending"] == len(body["tasks"])
    assert body["minutes"] == sum(t["minutes"] for t in body["tasks"])
    for t in body["tasks"]:
        assert t["blocked"] == ""                                                  # 别人的卡不出现
        assert t["situation"] and t["action"] and t["minutes"] >= 1 and t["impact_text"]
        assert "supplement" in t
