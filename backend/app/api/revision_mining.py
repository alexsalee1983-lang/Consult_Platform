"""三期 F4 最小版：改稿差异转候选。要求业务角色；只有项目成员能触发，非成员 404。

POST /api/projects/{code}/deliverables/{dcode}/revision-candidates
    对比各章 Agent 初稿与签署稿，返回候选常见坑、候选评审项（规则见 app/project/revision_mining.py）。
    开关 F4_REVISION_MINING 关（默认）或生产环境 404；严格档 422；未签署、客户不同意回流 409。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..models import User
from ..project import deliverables as dsvc
from ..project import revision_mining as svc

router = APIRouter(prefix="/api/projects", tags=["revision-mining"], dependencies=[Depends(require_business)])


@router.post("/{code}/deliverables/{dcode}/revision-candidates")
def revision_candidates(code: str, dcode: str, db: Session = Depends(get_db),
                        user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)
    try:
        d = dsvc.require_deliverable(db, p, dcode)
        return svc.mine(db, user, p, d)
    except (dsvc.DeliverableError, svc.MiningError) as e:
        raise HTTPException(e.status, getattr(e, "detail", str(e))) from e
