"""四期 A35：改稿（规则见 app/project/rework.py）。要求业务角色；只有项目成员能看能改，非成员一律 404。

GET  /api/projects/{code}/deliverables/{dcode}/review-list              审阅清单（按风险排序）、各章人工定稿 / 本轮变化 / 刷新轮数
POST /api/projects/{code}/deliverables/{dcode}/sections/{seq}/intent    本章最近一次人工修改的意图（事实 / 口径 / 措辞），事实类自动扩散
POST /api/projects/{code}/deliverables/{dcode}/refresh                  不满意刷新：{reason, seq?}；只有“写得不好”排后台重新起草（202）
POST /api/projects/{code}/deliverables/{dcode}/sections/{seq}/rollback  一键回退：{version}，旧版本正文写成新版本
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..models import User
from ..project import deliverables as dsvc
from ..project import rework

router = APIRouter(prefix="/api/projects", tags=["rework"], dependencies=[Depends(require_business)])


def _load(db: Session, code: str, dcode: str, user: User):
    p = project_for_member(db, code, user)
    try:
        return p, dsvc.require_deliverable(db, p, dcode)
    except dsvc.DeliverableError as e:
        raise HTTPException(e.status, e.detail) from e


def _run(fn, *args):
    try:
        return fn(*args)
    except (rework.ReworkError, dsvc.DeliverableError) as e:
        raise HTTPException(e.status, e.detail) from e


class RefreshIn(BaseModel):
    reason: str = Field(max_length=16)
    seq: int | None = Field(default=None, ge=1)


class RollbackIn(BaseModel):
    version: int = Field(ge=1)


@router.get("/{code}/deliverables/{dcode}/review-list")
def review_list(code: str, dcode: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p, d = _load(db, code, dcode, user)
    return rework.review_list(db, p, d)


@router.post("/{code}/deliverables/{dcode}/sections/{seq}/intent")
def edit_intent(code: str, dcode: str, seq: int, db: Session = Depends(get_db),
                user: User = Depends(current_user)) -> dict:
    p, d = _load(db, code, dcode, user)
    return _run(rework.detect, db, user, p, d, seq)


@router.post("/{code}/deliverables/{dcode}/refresh", status_code=202)
def refresh(code: str, dcode: str, body: RefreshIn, db: Session = Depends(get_db),
            user: User = Depends(current_user)) -> dict:
    p, d = _load(db, code, dcode, user)
    return _run(rework.refresh, db, user, p, d, body.reason, body.seq)


@router.post("/{code}/deliverables/{dcode}/sections/{seq}/rollback", status_code=201)
def rollback(code: str, dcode: str, seq: int, body: RollbackIn, db: Session = Depends(get_db),
             user: User = Depends(current_user)) -> dict:
    p, d = _load(db, code, dcode, user)
    _run(rework.rollback, db, user, p, d, seq, body.version)
    db.refresh(d)
    return {"seq": seq, "version": dsvc.require_section(d, seq).current_version}
