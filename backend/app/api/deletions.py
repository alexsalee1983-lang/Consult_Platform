"""项目资料删除（M4 ②）：规则见 app/project/deletion.py。

项目负责人（project_lead 角色，不必是项目成员）：
GET  /api/deletions?status=open|done|cancelled|all   删除确认队列（只有代号、档位、日期和计数）
GET  /api/deletions/{id}                             申请详情；待确认时附将删除的计数，完成后附删除记录
POST /api/deletions/{id}/confirm                     确认删除：必须选经验回流情况（已回流填投料编号）
POST /api/deletions/{id}/cancel                      撤销提前删除申请（写理由）；到期申请不能撤销
项目成员：
GET  /api/projects/{code}/deletions                  本项目的删除申请
POST /api/projects/{code}/deletions                  客户书面要求提前删除（项目经理）
PUT  /api/projects/{code}/flowback                   “客户不同意回流”标记（项目经理）
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..models import ProjectDeletion, User
from ..project import deletion as svc

router = APIRouter(prefix="/api", tags=["deletions"], dependencies=[Depends(require_business)])


class DeletionOut(BaseModel):
    id: int
    project_code: str
    tier: str
    kind: str
    status: str
    requested_by: str
    requested_at: datetime
    request_ref: str
    received_on: date | None
    due_by: date
    overdue: bool
    confirmed_by: str
    confirmed_at: datetime | None
    flowback: str
    flowback_refs: list[int]
    flowback_allowed: bool
    cancelled_by: str
    cancel_reason: str
    completed_at: datetime | None
    counts: dict


class DeletionDetail(DeletionOut):
    pending_counts: dict = {}
    record: dict | None = None


class EarlyIn(BaseModel):
    request_ref: str = Field(max_length=64)
    received_on: date


class ConfirmIn(BaseModel):
    flowback: str = Field(pattern="^(done|none|client_refused)$")
    flowback_refs: list[int] = Field(default_factory=list, max_length=svc.MAX_FLOWBACK_REFS)


class CancelIn(BaseModel):
    reason: str = Field(max_length=200)


class FlowbackIn(BaseModel):
    allowed: bool


def _out(d: ProjectDeletion, cls=DeletionOut):
    p = d.project
    return cls(id=d.id, project_code=p.code, tier=p.tier, kind=d.kind, status=d.status, requested_by=d.requested_by,
               requested_at=d.requested_at, request_ref=d.request_ref, received_on=d.received_on, due_by=d.due_by,
               overdue=d.status in svc.OPEN and date.today() > d.due_by, confirmed_by=d.confirmed_by,
               confirmed_at=d.confirmed_at, flowback=d.flowback, flowback_refs=d.flowback_refs or [],
               flowback_allowed=p.flowback_allowed, cancelled_by=d.cancelled_by, cancel_reason=d.cancel_reason,
               completed_at=d.completed_at, counts=d.counts or {})


def _err(e: svc.DeletionError):
    return HTTPException(e.status, e.detail)


@router.get("/deletions", response_model=list[DeletionOut])
def queue(status: str = Query("open", pattern="^(open|pending|confirmed|done|cancelled|all)$"),
          db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        return [_out(d) for d in svc.list_for_lead(db, user, status)]
    except svc.DeletionError as e:
        raise _err(e) from None


@router.get("/deletions/{deletion_id}", response_model=DeletionDetail)
def detail(deletion_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        d = svc.get_for_lead(db, user, deletion_id, "project.deletion.view")
    except svc.DeletionError as e:
        raise _err(e) from None
    out = _out(d, DeletionDetail)
    if d.status in svc.OPEN:
        out.pending_counts = svc.pending_counts(db, d.project)
    if d.status == svc.DONE:
        out.record = svc.record(d)
    return out


@router.post("/deletions/{deletion_id}/confirm", response_model=DeletionOut)
def confirm(deletion_id: int, body: ConfirmIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        return _out(svc.confirm(db, user, deletion_id, body.flowback, body.flowback_refs))
    except svc.DeletionError as e:
        raise _err(e) from None


@router.post("/deletions/{deletion_id}/cancel", response_model=DeletionOut)
def cancel(deletion_id: int, body: CancelIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        return _out(svc.cancel(db, user, deletion_id, body.reason))
    except svc.DeletionError as e:
        raise _err(e) from None


@router.get("/projects/{code}/deletions", response_model=list[DeletionOut])
def project_deletions(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    return [_out(d) for d in svc.list_for_project(db, p)]


@router.post("/projects/{code}/deletions", response_model=DeletionOut, status_code=201)
def request_early(code: str, body: EarlyIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    try:
        return _out(svc.request_early(db, user, p, body.request_ref, body.received_on))
    except svc.DeletionError as e:
        raise _err(e) from None


@router.put("/projects/{code}/flowback")
def set_flowback(code: str, body: FlowbackIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)
    try:
        svc.set_flowback_allowed(db, user, p, body.allowed)
    except svc.DeletionError as e:
        raise _err(e) from None
    return {"flowback_allowed": p.flowback_allowed}
