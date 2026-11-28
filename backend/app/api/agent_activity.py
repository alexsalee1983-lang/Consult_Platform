"""Agent 运行情况（四期 A29，《平台规划刷新设计》2.2）：页面右上角“查看 Agent 运行情况”按键背后的只读接口。

GET  /api/agent-activity/knowledge                知识侧运行记录（kb.* 后台任务）：知识管理员（可重跑）、咨询业务负责人（只读）
POST /api/agent-activity/knowledge/jobs/{id}/retry 重跑一条失败的知识侧任务：只给知识管理员；非知识侧任务 404，未失败 409
GET  /api/agent-activity/projects                 跨项目运行状态：只给咨询业务负责人，只看步骤和失败类型，不看内容

项目侧作业流在项目里看：全景 ?runs=1 多出“Agent 运行”一行、交付链抽屉（成员只读，重跑 / 暂停只给项目经理，
见 app/api/runs.py）；非成员 404。知识专家没有这个按键（403）。暂停来源走来源登记（/api/kb/sources/{code}/status）。
只返回编号、类别、状态、次数、失败类型和时间；不返回任务载荷、错误说明全文、交付件和章节内容。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import KNOWLEDGE_ADMIN, PROJECT_LEAD, require_business, roles_of
from ..models import AgentRun, Job, Project, User
from ..products import config
from ..project import configs
from ..services import jobs

router = APIRouter(prefix="/api/agent-activity", tags=["agent-activity"], dependencies=[Depends(require_business)])

KB_PREFIX = "kb."
LIMIT = 100


def failure_type(error: str) -> str:
    """失败类型 = 错误说明里第一个冒号前的部分（如 ValueError、ModelUnavailable），不给说明全文。"""
    head = error.replace("：", ":").split(":", 1)[0].strip()
    return head[:40]


class KbJobOut(BaseModel):
    id: int
    kind: str
    status: str
    attempts: int
    failure: str
    created_at: datetime
    finished_at: datetime | None


class KbActivityOut(BaseModel):
    can_manage: bool
    jobs: list[KbJobOut]


class ProjectRunOut(BaseModel):
    project: str
    status: str
    step: str
    step_name: str
    failure: str
    updated_at: datetime


def _job_out(j: Job) -> KbJobOut:
    return KbJobOut(id=j.id, kind=j.kind, status=j.status, attempts=j.attempts, failure=failure_type(j.last_error),
                    created_at=j.created_at, finished_at=j.finished_at)


@router.get("/knowledge", response_model=KbActivityOut)
def knowledge(db: Session = Depends(get_db), user: User = Depends(current_user)) -> KbActivityOut:
    roles = roles_of(db, user.id)
    if not roles & {KNOWLEDGE_ADMIN, PROJECT_LEAD}:
        raise HTTPException(403, "知识侧运行记录只给知识管理员和咨询业务负责人")
    rows = db.scalars(select(Job).where(Job.kind.startswith(KB_PREFIX), Job.project_code == "")
                      .order_by(Job.id.desc()).limit(LIMIT))
    return KbActivityOut(can_manage=KNOWLEDGE_ADMIN in roles, jobs=[_job_out(j) for j in rows])


@router.post("/knowledge/jobs/{job_id}/retry", response_model=KbJobOut)
def retry(job_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)) -> KbJobOut:
    if KNOWLEDGE_ADMIN not in roles_of(db, user.id):
        raise HTTPException(403, "只有知识管理员能重跑知识侧任务")
    job = db.get(Job, job_id)
    if job is None or not job.kind.startswith(KB_PREFIX) or job.project_code:
        raise HTTPException(404, "没有这条知识侧任务")
    if jobs.retry(db, job_id) is None:
        raise HTTPException(409, "只有失败的任务能重跑")
    audit.record(db, actor=user.username, action="agent_activity.retry", result="allowed", job_id=job_id,
                 kind=job.kind)
    db.refresh(job)
    return _job_out(job)


@router.get("/projects", response_model=list[ProjectRunOut])
def projects(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[ProjectRunOut]:
    if PROJECT_LEAD not in roles_of(db, user.id):
        raise HTTPException(403, "跨项目运行状态只给咨询业务负责人")
    out: list[ProjectRunOut] = []
    for p in db.scalars(select(Project).where(Project.deleted_at.is_(None)).order_by(Project.code)):
        run = db.scalar(select(AgentRun).where(AgentRun.project_id == p.id).order_by(AgentRun.id.desc()).limit(1))
        if run is None:
            continue
        key = name = ""
        try:
            chain = configs.snapshot(db, p)["chain"]
            if run.step_index < len(chain):
                key, name = chain[run.step_index]["key"], chain[run.step_index]["name"]
        except config.CatalogError:
            pass
        out.append(ProjectRunOut(project=p.code, status=run.status, step=key or str(run.step_index), step_name=name,
                                 failure=failure_type(run.error) if run.status == "failed" else "",
                                 updated_at=run.updated_at))
    return out
