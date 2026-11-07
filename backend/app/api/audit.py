"""审计日志查询：只有安全审计员能读。审计员只读，没有任何修改接口。"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import get_db
from ..core.permissions import require_auditor
from ..models import AuditLog
from ..schemas import AuditOut

router = APIRouter(prefix="/api", tags=["audit"], dependencies=[Depends(require_auditor)])


@router.get("/audit", response_model=list[AuditOut])
def recent_audit(limit: int = 50, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 500))
    return db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).all()
