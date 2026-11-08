"""审计日志查询与哈希链校验：只有安全审计员能调用。审计员只读，没有任何修改接口。"""
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import require_auditor
from ..models import AuditLog, User
from ..schemas import AuditOut

router = APIRouter(prefix="/api", tags=["audit"], dependencies=[Depends(require_auditor)])


class VerifyOut(BaseModel):
    ok: bool
    checked: int
    legacy: int
    head_id: int | None
    head_hash: str | None
    error_id: int | None
    error: str


@router.get("/audit", response_model=list[AuditOut])
def query_audit(
    db: Session = Depends(get_db),
    actor: str | None = Query(None, max_length=64),
    action: str | None = Query(None, max_length=32, description="前缀匹配，如 auth. 或 user."),
    result: str | None = Query(None, pattern="^(allowed|blocked|error)$"),
    project_code: str | None = Query(None, max_length=32),
    since: datetime | None = None,
    until: datetime | None = None,
    before_id: int | None = Query(None, description="翻页：只返回 id 小于它的记录"),
    limit: int = Query(50, ge=1, le=500),
):
    stmt = select(AuditLog)
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    if action:
        stmt = stmt.where(AuditLog.action.startswith(action, autoescape=True))
    if result:
        stmt = stmt.where(AuditLog.result == result)
    if project_code:
        stmt = stmt.where(AuditLog.project_code == project_code)
    if since:
        stmt = stmt.where(AuditLog.at >= since)
    if until:
        stmt = stmt.where(AuditLog.at < until)
    if before_id:
        stmt = stmt.where(AuditLog.id < before_id)
    return db.scalars(stmt.order_by(AuditLog.id.desc()).limit(limit)).all()


@router.get("/audit/verify", response_model=VerifyOut)
def verify(expect_head: str | None = Query(None, min_length=64, max_length=64, pattern="^[0-9a-f]{64}$",
                                           description="之前记下的链头哈希，校验它仍在链上"),
           db: Session = Depends(get_db), user: User = Depends(current_user)):
    r = audit.verify_chain(db, expect_head)
    audit.record(db, actor=user.username, action="audit.verify", result="allowed" if r.ok else "error",
                 checked=r.checked, head_id=r.head_id, error_id=r.error_id)
    return VerifyOut(**asdict(r))
