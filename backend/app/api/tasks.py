"""三期 B17②“我的任务”接口（规则见 app/project/my_tasks.py）。业务角色可用。

GET  /api/my-tasks          七类待办合一，按影响 × 不确定度排好，超出每日额度的折叠
POST /api/my-tasks/return   退回一项（记退回事件、排队由 Agent 重写）；不在本人待办里的 404
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core import audit
from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import require_business
from ..models import User
from ..project import my_tasks as mt
from ..services import guard

router = APIRouter(prefix="/api/my-tasks", tags=["my-tasks"], dependencies=[Depends(require_business)])


class FieldOut(BaseModel):
    name: str
    label: str
    value: str
    uncertain: bool


class TaskOut(BaseModel):
    kind: str
    ref_id: int
    title: str
    link: str
    project_code: str
    impact: float
    uncertainty: float
    score: float
    blocked: str
    folded: bool
    returnable: bool
    fields: list[FieldOut]


class KindOut(BaseModel):
    name: str
    available: bool
    count: int


class MyTasksOut(BaseModel):
    quota: int
    folded: int
    weights: dict[str, float]
    kinds: dict[str, KindOut]
    tasks: list[TaskOut]


class ReturnIn(BaseModel):
    kind: str = Field(max_length=24)
    ref_id: int
    reason: str = Field(min_length=1, max_length=200)


class ReturnOut(BaseModel):
    id: int
    status: str
    job_id: int | None


@router.get("", response_model=MyTasksOut)
def my_tasks(db: Session = Depends(get_db), user: User = Depends(current_user)) -> MyTasksOut:
    s = mt.summary(db, user)
    tasks = [TaskOut(kind=t.kind, ref_id=t.ref_id, title=t.title, link=t.link, project_code=t.project_code,
                     impact=t.impact, uncertainty=t.uncertainty, score=t.score, blocked=t.blocked, folded=t.folded,
                     returnable=t.kind in mt.RETURNABLE and not t.blocked,
                     fields=[FieldOut(**f) for f in t.fields]) for t in s["tasks"]]
    return MyTasksOut(quota=s["quota"], folded=s["folded"], weights=s["weights"],
                      kinds={k: KindOut(**v) for k, v in s["kinds"].items()}, tasks=tasks)


@router.post("/return", response_model=ReturnOut, status_code=202)
def return_task(body: ReturnIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> ReturnOut:
    if guard.check(body.reason).blocked:
        audit.record(db, actor=user.username, action="tasks.return", result="blocked", kind=body.kind,
                     ref_id=body.ref_id, length=len(body.reason), reason="guard")
        raise HTTPException(422, "退回意见含机密级内容，请删去后再提交")
    try:
        ret = mt.return_task(db, user, body.kind, body.ref_id, body.reason)
    except mt.TaskError as e:
        raise HTTPException(e.status, e.detail) from e
    audit.record(db, actor=user.username, action="tasks.return", result="ok", kind=body.kind, ref_id=body.ref_id,
                 length=len(body.reason), job_id=ret.job_id)
    return ReturnOut(id=ret.id, status=ret.status, job_id=ret.job_id)
