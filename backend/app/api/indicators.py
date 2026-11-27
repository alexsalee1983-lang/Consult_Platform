"""指标看板 v1（三期 F2①）：GET /api/indicators?days=30，只给项目负责人（其他角色 403）。
四期 F5：GET /api/projects/{code}/automation?days=7 自动化账本，项目成员可看。

只返回数字和口径，不返回项目代号、标题和正文；口径见 app/services/indicators.py。
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.config import get_settings
from ..core.permissions import PROJECT_LEAD, project_for_member, require_business, roles_of
from ..models import User
from ..services import automation_ledger, indicators, indicators_v4, quality_incidents

router = APIRouter(prefix="/api", tags=["indicators"], dependencies=[Depends(require_business)])


@router.get("/indicators")
def get_indicators(days: int = Query(30, ge=1, le=366), db: Session = Depends(get_db),
                   user: User = Depends(current_user)) -> dict:
    if PROJECT_LEAD not in roles_of(db, user.id):
        raise HTTPException(403, "指标看板只给项目负责人")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return {"days": days, **indicators.compute(db, start, end), "v4": indicators_v4.summary(db, start, end),  # 四期 F2③
            "quality_incidents": quality_incidents.summary(db, end),   # 四期 F3：质量事件只出计数
            "automation": {**automation_ledger.acceptance(db, start, end),     # 四期 F5：三条验收与自适应抽检
                           "spot": automation_ledger.adaptive_spot_rate(db, _base_rate())}}


def _base_rate() -> float:
    return float(getattr(get_settings(), "survey_spot_rate", 0.1))   # E3① 合并前没有该配置项，按 10%


@router.get("/projects/{code}/automation")
def project_automation(code: str, days: int = Query(7, ge=1, le=366), db: Session = Depends(get_db),
                       user: User = Depends(current_user)) -> dict:
    """四期 F5：驾驶舱页头“本周自动 N / 人工 M / 抽检差错 K”与自动条目；只有项目成员能看，非成员 404。"""
    p = project_for_member(db, code, user)
    end = datetime.now(timezone.utc)
    return {"days": days, **automation_ledger.ledger(db, end - timedelta(days=days), end, [p.code])}
