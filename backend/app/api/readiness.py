"""就绪度看板（三期 F2②）：GET /api/kb/readiness?days=90，给知识专家、知识管理员和咨询业务负责人（其他 403）。

只返回章节编号与标题、知识 key、条目类型和计数，不含条目正文；口径见 app/kb/readiness.py。
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db, utcnow
from ..core.permissions import KNOWLEDGE_ADMIN, KNOWLEDGE_EXPERT, PROJECT_LEAD, require_business, roles_of
from ..kb import readiness
from ..models import User
from ..services import indicators_v4

router = APIRouter(prefix="/api/kb", tags=["kb"], dependencies=[Depends(require_business)])
ALLOWED = frozenset({KNOWLEDGE_EXPERT, KNOWLEDGE_ADMIN, PROJECT_LEAD})


@router.get("/readiness")
def get_readiness(days: int = Query(90, ge=1, le=366), db: Session = Depends(get_db),
                  user: User = Depends(current_user)) -> dict:
    if not roles_of(db, user.id) & ALLOWED:
        raise HTTPException(403, "就绪度看板只给知识专家、知识管理员和咨询业务负责人")
    end = utcnow()
    m = readiness.matrix(db)
    return {"days": days, **m, "grids": indicators_v4.readiness_grids(m["evalsets"]),   # 四期 F2③ 四格就绪度与目标
            "experts": readiness.expert_metrics(db, end - timedelta(days=days), end)}
