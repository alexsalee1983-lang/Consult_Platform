"""AI 质量事件台账接口（四期 F3）。规则在 app/services/quality_incidents.py。

- POST /api/quality-incidents：登记（业务角色；挂项目须是成员，否则 404）；
- GET  /api/quality-incidents：台账（只给咨询业务负责人），可按 status=open/closed、level 过滤；
- GET  /api/quality-incidents/{id}：详情与处置记录（咨询业务负责人、所属项目经理、登记人；其他人 404）；
- POST /api/quality-incidents/{id}/events：追加一步处置（咨询业务负责人、所属项目经理）。
描述与说明只记长度和摘要哈希，接口不回显原文。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import PROJECT_LEAD, require_any, require_business
from ..models import User
from ..services import quality_incidents as svc

router = APIRouter(prefix="/api/quality-incidents", tags=["quality-incidents"],
                   dependencies=[Depends(require_business)])


class IncidentIn(BaseModel):
    category: str = Field(max_length=32)
    ref_kind: str = Field(max_length=16)
    ref_id: str = Field(min_length=1, max_length=64)
    project: str = Field(default="", max_length=32)
    description: str = Field(default="", max_length=svc.NOTE_MAX)


class EventIn(BaseModel):
    step: str = Field(max_length=16)
    level: str = Field(default="", max_length=4)
    action: str = Field(default="", max_length=24)
    root_cause: str = Field(default="", max_length=16)
    note: str = Field(default="", max_length=svc.NOTE_MAX)
    agent: str = Field(default="", max_length=32)        # 回滚 Agent 版本：Agent 代号与目标版本哈希
    version: str = Field(default="", max_length=12)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.post("", status_code=201)
def register(body: IncidentIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    try:
        return svc.register(db, user, **body.model_dump()).out(_now())
    except svc.IncidentError as e:
        raise HTTPException(e.status, str(e)) from None


@router.get("", dependencies=[Depends(require_any(PROJECT_LEAD))])
def list_incidents(status: str = Query(default="", pattern="^(|open|closed)$"),
                   level: str = Query(default="", pattern="^(|P1|P2|P3)$"), db: Session = Depends(get_db)) -> dict:
    now = _now()
    items = [v.out(now) for v in svc.all_views(db)]
    if status:
        items = [i for i in items if (i["status"] == "closed") == (status == "closed")]
    if level:
        items = [i for i in items if i["level"] == level]
    return {"items": items, "summary": svc.summary(db, now)}


@router.get("/{incident_id}")
def detail(incident_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    try:
        v = svc.get(db, user, incident_id)
        return {**v.out(_now(), with_events=True), "impact": svc.impact(db, user, v)}
    except svc.IncidentError as e:
        raise HTTPException(e.status, str(e)) from None


@router.post("/{incident_id}/events", status_code=201)
def add_event(incident_id: int, body: EventIn, db: Session = Depends(get_db),
              user: User = Depends(current_user)) -> dict:
    try:
        return svc.add_event(db, user, incident_id, **body.model_dump()).out(_now())
    except svc.IncidentError as e:
        raise HTTPException(e.status, str(e)) from None
