"""四期 F4②：改稿与驳回特征汇总。只给咨询业务负责人、知识管理员（其他角色 403）。

GET /api/revision-features/summary?project=CODE
    按章节键、来源聚合计数（规则见 app/project/revision_features.py）；不带 project 时汇总本人所在的全部项目，
    带 project 时本人须是该项目成员（非成员与不存在一样 404）。开关 F4_REVISION_MINING 关（默认）或生产环境 404。
    只读，不写审计；只返回计数和改动幅度，不返回正文。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import KNOWLEDGE_ADMIN, PROJECT_LEAD, member_project_ids, project_for_member, require_any
from ..models import Project, User
from ..project import revision_features as svc
from ..project.revision_mining import enabled

router = APIRouter(prefix="/api/revision-features", tags=["revision-features"],
                   dependencies=[Depends(require_any(PROJECT_LEAD, KNOWLEDGE_ADMIN))])


@router.get("/summary")
def summary(project: str | None = Query(None, max_length=32), db: Session = Depends(get_db),
            user: User = Depends(current_user)) -> dict:
    if not enabled():
        raise HTTPException(404, "改稿与驳回特征未开启（只在测试机、试用机上开）")
    if project:
        projects = [project_for_member(db, project, user)]
    else:
        ids = member_project_ids(db, user.id)
        projects = list(db.scalars(select(Project).where(Project.id.in_(ids)))) if ids else []
    return {"project": project or None, **svc.summary(db, projects)}
