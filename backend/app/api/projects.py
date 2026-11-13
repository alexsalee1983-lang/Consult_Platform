"""项目、待确认事项（确认卡）、项目成员。全部要求业务角色；项目数据只对项目成员可见，非成员一律 404。

确认卡（M3）：GET /api/confirmations 我参与的项目里待处理的卡；GET /api/projects/{code}/confirmations?status=
某项目的卡（open 待处理 / resolved 已处理 / all）；POST /api/confirmations/{id}/resolve 处理（选选项或写意见），
规则见 app/project/confirmations.py。

保留期（M4 ①）：POST /api/projects/{code}/acceptance 项目经理登记验收日和保留月数；项目列表和详情带
retention_state、deletion_due_on、days_to_deletion，到期前 30 天显示提醒。规则见 app/project/retention.py。
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..core import audit
from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import (
    BUSINESS_ROLES,
    PROJECT_MANAGER,
    membership,
    project_for_member,
    require_business,
    roles_of,
)
from ..models import Confirmation, Project, ProjectMember, User
from ..project import confirmations as conf_svc
from ..project import retention
from ..project import runs as run_svc
from ..schemas import PHASES, ConfirmationOut, ProjectDetail, ProjectOut, ResolveIn

router = APIRouter(prefix="/api", tags=["projects"], dependencies=[Depends(require_business)])


def _retention(data, p: Project):
    st = retention.status(p)
    data.retention_state, data.days_to_deletion = st.state, st.days_left
    return data


def _out(p: Project, cls=ProjectOut):
    data = cls.model_validate(p, from_attributes=True)
    data.phase_name = PHASES[p.phase] if 0 <= p.phase < len(PHASES) else ""
    _retention(data, p)
    data.open_confirmations = sum(1 for c in p.confirmations if c.status == "open")
    return data


def _conf_out(c: Confirmation) -> ConfirmationOut:
    o = ConfirmationOut.model_validate(c, from_attributes=True)
    o.project_code = c.project.code
    return o


def _my_projects(user: User):
    return select(Project).join(ProjectMember, ProjectMember.project_id == Project.id) \
        .where(ProjectMember.user_id == user.id)


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(_my_projects(user).options(selectinload(Project.confirmations)).order_by(Project.code)).all()
    return [_out(p) for p in rows]


@router.get("/projects/{code}", response_model=ProjectDetail)
def get_project(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user, selectinload(Project.deliverables), selectinload(Project.confirmations))
    detail = ProjectDetail.model_validate(p, from_attributes=True)
    detail.phase_name = PHASES[p.phase] if 0 <= p.phase < len(PHASES) else ""
    detail.open_confirmations = sum(1 for c in p.confirmations if c.status == "open")
    detail.confirmations = [_conf_out(c) for c in p.confirmations if c.status == "open"]
    return _retention(detail, p)


class AcceptanceIn(BaseModel):
    accepted_on: date
    retention_months: int = Field(default=retention.DEFAULT_MONTHS)


@router.post("/projects/{code}/acceptance", response_model=ProjectOut)
def set_acceptance(code: str, body: AcceptanceIn, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    """项目经理登记（或更正）验收日和保留期；规则见 app/project/retention.py。"""
    p = project_for_member(db, code, user, selectinload(Project.confirmations))
    try:
        retention.accept(db, user, p, body.accepted_on, body.retention_months)
    except retention.RetentionError as e:
        raise HTTPException(e.status, e.detail) from None
    return _out(p)


@router.get("/confirmations", response_model=list[ConfirmationOut])
def list_confirmations(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(select(Confirmation).join(ProjectMember, ProjectMember.project_id == Confirmation.project_id)
                      .where(Confirmation.status == "open", ProjectMember.user_id == user.id)
                      .options(selectinload(Confirmation.project)).order_by(Confirmation.id)).all()
    return [_conf_out(c) for c in rows]


@router.get("/projects/{code}/confirmations", response_model=list[ConfirmationOut])
def project_confirmations(code: str, status: str = Query(default="all", pattern="^(open|resolved|all)$"),
                          db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    q = select(Confirmation).where(Confirmation.project_id == p.id)
    if status != "all":
        q = q.where(Confirmation.status == status)
    return [_conf_out(c) for c in db.scalars(q.options(selectinload(Confirmation.project))
                                             .order_by(Confirmation.id.desc()))]


@router.post("/confirmations/{cid}/resolve", response_model=ConfirmationOut)
def resolve(cid: int, body: ResolveIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = db.get(Confirmation, cid)
    if c is None or membership(db, c.project_id, user.id) is None:
        raise HTTPException(404, "待确认事项不存在")
    try:
        c = conf_svc.resolve(db, user, c, body.choice, body.resolution)
    except conf_svc.ConfirmationError as e:
        raise HTTPException(e.status, e.detail) from e
    run_svc.on_card_resolved(db, c)   # 交付链在等这张卡时，卡都处理完就继续
    return _conf_out(c)


# ---- 项目成员：成员可查看；只有本项目的项目经理能增删改 ----

class MemberOut(BaseModel):
    username: str
    display_name: str
    project_role: str


class MemberIn(BaseModel):
    project_role: str = Field(pattern="^(manager|member)$")


def _members(db: Session, p: Project) -> list[MemberOut]:
    rows = db.execute(select(User, ProjectMember.project_role).join(ProjectMember, ProjectMember.user_id == User.id)
                      .where(ProjectMember.project_id == p.id).order_by(User.username)).all()
    return [MemberOut(username=u.username, display_name=u.display_name or u.username, project_role=r) for u, r in rows]


def _require_manager(db: Session, p: Project, user: User) -> None:
    m = membership(db, p.id, user.id)
    if m is None or m.project_role != PROJECT_MANAGER:
        audit.record(db, actor=user.username, action="project.member", result="blocked", project_code=p.code,
                     reason="not_manager")
        raise HTTPException(403, "只有本项目的项目经理能调整成员")


def _managers(db: Session, p: Project) -> int:
    return db.scalar(select(func.count()).select_from(ProjectMember)
                     .where(ProjectMember.project_id == p.id, ProjectMember.project_role == PROJECT_MANAGER))


@router.get("/projects/{code}/members", response_model=list[MemberOut])
def list_members(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _members(db, project_for_member(db, code, user))


@router.put("/projects/{code}/members/{username}", response_model=list[MemberOut])
def put_member(code: str, username: str, body: MemberIn, db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    _require_manager(db, p, user)
    target = db.scalar(select(User).where(User.username == username))
    if target is None or not target.is_active:
        raise HTTPException(404, "用户不存在或已停用")
    if not roles_of(db, target.id) & BUSINESS_ROLES:
        raise HTTPException(422, "只有持业务角色（顾问、项目负责人、知识专家、知识管理员）的用户能加入项目")
    m = membership(db, p.id, target.id)
    if m is not None and m.project_role == PROJECT_MANAGER and body.project_role != PROJECT_MANAGER \
            and _managers(db, p) == 1:
        raise HTTPException(422, "项目至少要保留一名项目经理")
    if m is None:
        db.add(ProjectMember(project_id=p.id, user_id=target.id, project_role=body.project_role,
                             added_by=user.username))
    else:
        m.project_role = body.project_role
    db.commit()
    audit.record(db, actor=user.username, action="project.member", result="allowed", project_code=p.code,
                 target=username, project_role=body.project_role)
    return _members(db, p)


@router.delete("/projects/{code}/members/{username}", response_model=list[MemberOut])
def delete_member(code: str, username: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    _require_manager(db, p, user)
    target = db.scalar(select(User).where(User.username == username))
    m = membership(db, p.id, target.id) if target else None
    if m is None:
        raise HTTPException(404, "该用户不是本项目成员")
    if m.project_role == PROJECT_MANAGER and _managers(db, p) == 1:
        raise HTTPException(422, "项目至少要保留一名项目经理")
    db.delete(m)
    db.commit()
    audit.record(db, actor=user.username, action="project.member_remove", result="allowed", project_code=p.code,
                 target=username)
    return _members(db, p)
