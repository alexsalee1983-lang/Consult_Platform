"""指标看板 v1（三期 F2①）：GET /api/indicators?days=30，只给项目负责人（其他角色 403）。

只返回数字和口径，不返回项目代号、标题和正文；口径见 app/services/indicators.py。
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import PROJECT_LEAD, require_business, roles_of
from ..models import User
from ..services import indicators, indicators_v4, quality_incidents

router = APIRouter(prefix="/api", tags=["indicators"], dependencies=[Depends(require_business)])


@router.get("/indicators")
def get_indicators(days: int = Query(30, ge=1, le=366), db: Session = Depends(get_db),
                   user: User = Depends(current_user)) -> dict:
    if PROJECT_LEAD not in roles_of(db, user.id):
        raise HTTPException(403, "指标看板只给项目负责人")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return {"days": days, **indicators.compute(db, start, end), "v4": indicators_v4.summary(db, start, end),  # 四期 F2③
            "quality_incidents": quality_incidents.summary(db, end)}   # 四期 F3：质量事件只出计数
