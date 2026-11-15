"""项目总览（负责人 11-15 定方案 A）：项目负责人看全部项目的元数据，不必是成员。

GET /api/portfolio   只给项目负责人（其他角色 403）。每个项目只返回代号、产品、行业、档位、阶段、周次、项目经理、
                     成员数、交付件状态计数、保留期与删除状态、本人是否成员；不返回资料、纪要、交付件标题和正文、
                     确认卡、对话。进入驾驶舱仍按成员判断（非成员 404），见 app/core/permissions.py 规则 6。
"""
from collections import Counter
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import PROJECT_LEAD, PROJECT_MANAGER, require_business, roles_of
from ..models import Project, ProjectDeletion, ProjectMember, User
from ..project import configs, retention
from ..schemas import PHASES

router = APIRouter(prefix="/api", tags=["portfolio"], dependencies=[Depends(require_business)])


class PortfolioItem(BaseModel):
    code: str
    product_name: str = ""
    industry: str
    tier: str
    phase_name: str = ""
    week: int
    total_weeks: int
    managers: list[str]
    member_count: int
    deliverables: dict[str, int]          # 状态 → 个数（not_started / drafting / review / signed）
    retention_state: str
    deletion_due_on: date | None = None
    deletion_status: str = ""             # 最近一条删除申请：pending / confirmed / done / cancelled；空 = 没有
    deleted: bool = False
    is_member: bool


@router.get("/portfolio", response_model=list[PortfolioItem])
def portfolio(db: Session = Depends(get_db), user: User = Depends(current_user)):
    if PROJECT_LEAD not in roles_of(db, user.id):
        raise HTTPException(403, "项目总览只给项目负责人")
    projects = db.scalars(select(Project).options(selectinload(Project.deliverables)).order_by(Project.code)).all()
    members = db.execute(select(ProjectMember.project_id, ProjectMember.user_id, ProjectMember.project_role, User.display_name,
                                User.username).join(User, User.id == ProjectMember.user_id)).all()
    latest: dict[int, str] = {}
    for pid, st in db.execute(select(ProjectDeletion.project_id, ProjectDeletion.status).order_by(ProjectDeletion.id)):
        latest[pid] = st
    out = []
    for p in projects:
        mine = [m for m in members if m.project_id == p.id]
        info = configs.info(db, p)
        out.append(PortfolioItem(
            code=p.code, product_name=info.get("product_name") or p.service, industry=p.industry, tier=p.tier,
            phase_name=PHASES[p.phase] if 0 <= p.phase < len(PHASES) else "", week=p.week, total_weeks=p.total_weeks,
            managers=[m.display_name or m.username for m in mine if m.project_role == PROJECT_MANAGER],
            member_count=len(mine), deliverables=dict(Counter(d.status for d in p.deliverables)),
            retention_state=retention.status(p).state, deletion_due_on=p.deletion_due_on,
            deletion_status=latest.get(p.id, ""), deleted=p.deleted_at is not None,
            is_member=any(m.user_id == user.id for m in mine)))
    return out
