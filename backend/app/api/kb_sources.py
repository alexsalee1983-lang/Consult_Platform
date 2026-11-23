"""来源登记表接口（三期 B16）。只限知识专家、知识管理员；规则在 app/kb/source_registry.py。

GET   /api/kb/sources[?status=]          列表（等级高的在前）
POST  /api/kb/sources                    登记候选来源（许可、权威类别必填）
PATCH /api/kb/sources/{code}             修改字段
POST  /api/kb/sources/{code}/status      状态流转 {to, reason}；转正式只限知识管理员、不能是登记人
GET   /api/kb/sources/{code}/events      状态变更记录
"""
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import require_business
from ..kb import review as rv
from ..kb import source_registry as reg
from ..models import KbSource, User

router = APIRouter(prefix="/api/kb/sources", tags=["kb"], dependencies=[Depends(require_business)])


class SourceIn(BaseModel):
    name: str = Field(max_length=200)
    url: str = Field(max_length=500)
    license: str = Field(max_length=300)
    authority_kind: str = Field(max_length=16)
    publisher: str = Field("", max_length=200)
    note: str = Field("", max_length=2000)


class SourcePatch(BaseModel):
    name: str | None = Field(None, max_length=200)
    url: str | None = Field(None, max_length=500)
    license: str | None = Field(None, max_length=300)
    authority_kind: str | None = Field(None, max_length=16)
    publisher: str | None = Field(None, max_length=200)
    note: str | None = Field(None, max_length=2000)


class StatusIn(BaseModel):
    to: str = Field(max_length=12)
    reason: str = Field("", max_length=300)


class SourceOut(BaseModel):
    code: str
    name: str
    url: str
    host: str
    publisher: str
    license: str
    authority_kind: str
    authority_level: int
    status: str
    note: str
    created_by: str
    approved_by: str
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EventOut(BaseModel):
    from_status: str
    to_status: str
    reason: str
    actor: str
    created_at: datetime


def _staff(db: Session, user: User) -> None:
    if not rv.is_knowledge_user(db, user):
        raise HTTPException(403, "只有知识专家、知识管理员能管理来源登记表")


def _out(s: KbSource) -> SourceOut:
    return SourceOut.model_validate(s, from_attributes=True)


def _run(fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
    try:
        return fn(*args, **kw)
    except reg.SourceError as e:
        raise HTTPException(e.status, e.detail) from None


@router.get("", response_model=list[SourceOut])
def list_sources(status: str = Query("", max_length=12), db: Session = Depends(get_db),
                 user: User = Depends(current_user)) -> list[SourceOut]:
    _staff(db, user)
    return [_out(s) for s in reg.list_sources(db, status)]


@router.post("", response_model=SourceOut, status_code=201)
def create(body: SourceIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> SourceOut:
    _staff(db, user)
    return _out(_run(reg.create, db, user, **body.model_dump()))


@router.patch("/{code}", response_model=SourceOut)
def update(code: str, body: SourcePatch, db: Session = Depends(get_db),
           user: User = Depends(current_user)) -> SourceOut:
    _staff(db, user)
    return _out(_run(reg.update, db, user, code, body.model_dump()))


@router.post("/{code}/status", response_model=SourceOut)
def transition(code: str, body: StatusIn, db: Session = Depends(get_db),
               user: User = Depends(current_user)) -> SourceOut:
    _staff(db, user)
    return _out(_run(reg.transition, db, user, code, body.to, body.reason))


@router.get("/{code}/events", response_model=list[EventOut])
def events(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[EventOut]:
    _staff(db, user)
    return [EventOut.model_validate(e, from_attributes=True) for e in _run(reg.events, db, code)]
