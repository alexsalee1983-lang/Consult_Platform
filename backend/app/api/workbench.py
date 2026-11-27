"""工作台兼容接口（四期试用反馈 11-25：工作台已并入“我的任务”）。

GET /api/workbench   业务角色可用。保留给旧链接和旧脚本；不再单独统计，待办直接取“我的任务”的同一份
（app/project/my_tasks.collect），计数口径与 /api/my-tasks 一致。类名照旧：待签署仍叫 signoff。
本人不能处理的照样列出，blocked 写明要谁处理。规则本身不在这里判断，各页提交时仍按原有规则校验。
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import require_business, roles_of
from ..models import User
from ..project import my_tasks as mt

router = APIRouter(prefix="/api", tags=["workbench"], dependencies=[Depends(require_business)])

LEGACY_KIND = {"deliverable_review": "signoff"}


class Todo(BaseModel):
    kind: str            # confirmation / signoff / kb_content / kb_compliance / recheck / deid / deletion / sample
    title: str
    project_code: str = ""
    link: str
    blocked: str = ""    # 非空 = 本人不能处理，写要谁处理


class WorkbenchOut(BaseModel):
    roles: list[str]
    member_projects: int
    todos: list[Todo]


@router.get("/workbench", response_model=WorkbenchOut)
def workbench(db: Session = Depends(get_db), user: User = Depends(current_user)) -> WorkbenchOut:
    todos = [Todo(kind=LEGACY_KIND.get(t.kind, t.kind), title=t.title, project_code=t.project_code, link=t.link,
                  blocked=t.blocked) for t in mt.collect(db, user)]
    return WorkbenchOut(roles=sorted(roles_of(db, user.id)), member_projects=mt.member_projects(db, user),
                        todos=todos)
