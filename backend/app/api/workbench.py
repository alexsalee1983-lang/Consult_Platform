"""工作台（负责人 11-15 定界面统筹方案）：把一个人身上各角色的待办汇总到一处。

GET /api/workbench   业务角色可用。只汇总本人能看到的东西：
  - 确认卡：本人所在项目里未处理的卡（顾问）
  - 待签署：本人任项目经理的项目里待签署的交付件
  - 知识审核：待审版本里本人角色对应、还没人通过的审核项（内容审核 = 知识专家，合规审核 = 知识管理员）
  - 去标识确认：待确认的上传（知识管理员）
  - 补复审：标 single_review 的已发布版本（知识专家）
  - 资料删除：待确认的删除申请（项目负责人，不必是成员；只给项目代号和日期）
本人不能处理的（作者不能审自己的版本、上传者不能确认去标识、发起人不能确认删除）照样列出，
blocked 写明要谁处理，界面置灰。规则本身不在这里判断，各页提交时仍按原有规则校验。
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import (KNOWLEDGE_ADMIN, KNOWLEDGE_EXPERT, PROJECT_LEAD, PROJECT_MANAGER, require_business,
                                roles_of)
from ..kb import review as rv
from ..models import Confirmation, Deliverable, Project, ProjectDeletion, ProjectMember, User
from ..models.kb import KbUpload, KbVersion

router = APIRouter(prefix="/api", tags=["workbench"], dependencies=[Depends(require_business)])


class Todo(BaseModel):
    kind: str            # confirmation / signoff / kb_content / kb_compliance / deid / recheck / deletion
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
    roles = roles_of(db, user.id)
    me = user.username
    todos: list[Todo] = []
    live = db.execute(select(ProjectMember.project_id, ProjectMember.project_role, Project.code)
                      .join(Project, Project.id == ProjectMember.project_id)
                      .where(ProjectMember.user_id == user.id, Project.deleted_at.is_(None))).all()
    member_ids = [m.project_id for m in live]
    managed = {m.project_id for m in live if m.project_role == PROJECT_MANAGER}
    codes = {m.project_id: m.code for m in live}

    if member_ids:
        for c in db.scalars(select(Confirmation).where(Confirmation.status == "open",
                                                       Confirmation.project_id.in_(member_ids))
                            .order_by(Confirmation.id)):
            code = codes[c.project_id]
            todos.append(Todo(kind="confirmation", title=c.title, project_code=code, link=f"/project/{code}"))
    if managed:
        for d in db.scalars(select(Deliverable).where(Deliverable.status == "review",
                                                      Deliverable.project_id.in_(managed))
                            .order_by(Deliverable.project_id, Deliverable.code)):
            code = codes[d.project_id]
            todos.append(Todo(kind="signoff", title=f"{d.code} {d.title}", project_code=code,
                              link=f"/project/{code}/deliverables/{d.code}"))

    if roles & rv.KNOWLEDGE_ROLES:
        for v in db.scalars(select(KbVersion).where(KbVersion.status == "in_review")
                            .options(selectinload(KbVersion.item), selectinload(KbVersion.reviews))
                            .order_by(KbVersion.submitted_at)):
            done = {r.review_role for r in v.reviews if r.decision == "approve"}
            mine = me in rv._authors(v) and not rv.self_review_allowed(v)
            for role, need in rv.ROLE_OF.items():
                if need in roles and role not in done:
                    other = "知识专家" if need == KNOWLEDGE_EXPERT else "知识管理员"
                    todos.append(Todo(kind=f"kb_{role}", title=f"{v.item.doc_id} v{v.version} {v.title}",
                                      link="/kb/review", blocked=f"你是作者，需另一位{other}处理" if mine else ""))
        if KNOWLEDGE_EXPERT in roles:
            for v in db.scalars(select(KbVersion).where(KbVersion.status == "published",
                                                        KbVersion.single_review.is_(True))
                                .options(selectinload(KbVersion.item))):
                todos.append(Todo(kind="recheck", title=f"{v.item.doc_id} v{v.version} {v.title}", link="/kb/review",
                                  blocked="你是作者，需另一位知识专家复审" if me in rv._authors(v) else ""))
    if KNOWLEDGE_ADMIN in roles:
        for u in db.scalars(select(KbUpload).where(KbUpload.status == "deid_pending").order_by(KbUpload.id)):
            todos.append(Todo(kind="deid", title=f"上传 #{u.id}", link="/kb/intake",
                              blocked="你是上传者，需另一位知识管理员确认" if u.uploaded_by == me else ""))

    if PROJECT_LEAD in roles:
        for d in db.scalars(select(ProjectDeletion).where(ProjectDeletion.status == "pending")
                            .options(selectinload(ProjectDeletion.project)).order_by(ProjectDeletion.id)):
            todos.append(Todo(kind="deletion", title=f"删除申请 #{d.id}", project_code=d.project.code,
                              link="/deletions",
                              blocked="你是发起人，需另一位项目负责人确认" if d.requested_by == me else ""))
    return WorkbenchOut(roles=sorted(roles), member_projects=len(live), todos=todos)
