"""投毒防护与处置接口（四期 B42）。规则在 app/kb/poison.py。

- GET  /api/kb/poison：事件列表（知识管理员、咨询业务负责人），可按 status 过滤；
- GET  /api/kb/poison/{id}：详情，含受影响条目、引用计数、提交人记录；
- POST /api/kb/poison/report：报告疑似投毒，当场冻结（知识专家、知识管理员、咨询业务负责人）；
- POST /api/kb/poison/{id}/propose：知识管理员提处置意见（clear / partial / restore）；超时未处理时负责人也可提；
- POST /api/kb/poison/{id}/decide：咨询业务负责人批准或驳回，提意见的人不能自己批；
- POST /api/kb/poison/{id}/notice：提交过快提醒，负责人关闭或转为冻结；
- POST /api/kb/poison/versions/{vid}/corroborate：知识专家确认已有第二来源，清除单一来源降权。
说明只记长度和摘要哈希，接口不回显原文。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import require_business
from ..kb import poison as svc
from ..models import User

router = APIRouter(prefix="/api/kb/poison", tags=["kb-poison"], dependencies=[Depends(require_business)])


class ReportIn(BaseModel):
    doc_id: str = Field(default="", max_length=40)
    batch: str = Field(default="", max_length=64)
    submitter: str = Field(default="", max_length=64)
    note: str = Field(default="", max_length=svc.NOTE_MAX)


class ProposeIn(BaseModel):
    decision: str = Field(max_length=12)
    doc_ids: list[str] = Field(default_factory=list, max_length=500)
    note: str = Field(default="", max_length=svc.NOTE_MAX)


class DecideIn(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=svc.NOTE_MAX)


class NoticeIn(BaseModel):
    action: str = Field(max_length=12)


def _call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except svc.PoisonError as e:
        raise HTTPException(e.status, str(e)) from e


@router.get("")
def listing(status: str = Query(default="", max_length=12), db: Session = Depends(get_db),
            user: User = Depends(current_user)) -> list[dict]:
    return _call(svc.listing, db, user, status)


@router.post("/report")
def report(body: ReportIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    inc = _call(svc.report, db, user, doc_id=body.doc_id.strip(), batch_key=body.batch, submitter=body.submitter,
                note=body.note)
    return svc.out(db, inc)


@router.post("/versions/{version_id}/corroborate")
def corroborate(version_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    v = _call(svc.corroborate, db, user, version_id)
    return {"version_id": v.id, "single_source": v.single_source}


@router.get("/{inc_id}")
def detail(inc_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return _call(svc.detail, db, user, inc_id)


@router.post("/{inc_id}/propose")
def propose(inc_id: int, body: ProposeIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return svc.out(db, _call(svc.propose, db, user, inc_id, body.decision, body.doc_ids, body.note))


@router.post("/{inc_id}/decide")
def decide(inc_id: int, body: DecideIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return svc.out(db, _call(svc.decide, db, user, inc_id, body.approve, body.note))


@router.post("/{inc_id}/notice")
def notice(inc_id: int, body: NoticeIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return svc.out(db, _call(svc.notice_action, db, user, inc_id, body.action))
