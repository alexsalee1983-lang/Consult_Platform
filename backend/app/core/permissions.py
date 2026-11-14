"""权限：系统角色、三权分立规则、项目成员检查。

规则（改动这里必须同步更新 tests/test_permissions.py 的权限矩阵）：
1. 业务角色：顾问、项目负责人、知识专家、知识管理员。只有持业务角色的用户能访问业务接口。
2. 系统管理员：只管账号和角色，看不到任何业务数据（项目、对话、待确认、审计）。
3. 安全审计员：只读审计日志。由单独任命，只能在服务器命令行授予或撤销（python -m app.cli），
   任何接口都不能授予、撤销，也不能修改审计员账号。
4. 系统管理员、安全审计员与其他任何角色互斥：一个账号只能是其中之一，且不能兼任业务角色。
5. 系统管理员不能修改自己的角色、不能停用自己。
6. 项目可见性只看 project_members：不是成员一律返回 404（不暴露项目是否存在）。
7. 项目内身份（项目经理 / 项目成员）放在 project_members，不是系统角色。
"""
from __future__ import annotations

from collections.abc import Iterable

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Project, ProjectMember, User, UserRole
from .auth import current_user
from .db import get_db

CONSULTANT = "consultant"
PROJECT_LEAD = "project_lead"
KNOWLEDGE_EXPERT = "knowledge_expert"
KNOWLEDGE_ADMIN = "knowledge_admin"
SYS_ADMIN = "sys_admin"
SECURITY_AUDITOR = "security_auditor"

ROLE_NAMES = {
    CONSULTANT: "顾问", PROJECT_LEAD: "项目负责人", KNOWLEDGE_EXPERT: "知识专家",
    KNOWLEDGE_ADMIN: "知识管理员", SYS_ADMIN: "系统管理员", SECURITY_AUDITOR: "安全审计员",
}
BUSINESS_ROLES = frozenset({CONSULTANT, PROJECT_LEAD, KNOWLEDGE_EXPERT, KNOWLEDGE_ADMIN})
EXCLUSIVE_ROLES = frozenset({SYS_ADMIN, SECURITY_AUDITOR})   # 三权分立：与其他任何角色互斥
ALL_ROLES = frozenset(ROLE_NAMES)

PROJECT_MANAGER = "manager"
PROJECT_MEMBER = "member"
PROJECT_ROLES = {PROJECT_MANAGER: "项目经理", PROJECT_MEMBER: "项目成员"}

NOT_FOUND = "项目不存在"


class RoleRuleError(ValueError):
    pass


def roles_of(db: Session, user_id: int) -> set[str]:
    return set(db.scalars(select(UserRole.role_code).where(UserRole.user_id == user_id)))


def check_role_set(roles: Iterable[str]) -> None:
    roles = set(roles)
    unknown = roles - ALL_ROLES
    if unknown:
        raise RoleRuleError(f"未知角色：{', '.join(sorted(unknown))}")
    exclusive = roles & EXCLUSIVE_ROLES
    if exclusive and len(roles) > 1:
        name = ROLE_NAMES[next(iter(exclusive))]
        raise RoleRuleError(f"{name}与其他角色互斥（三权分立），不能兼任")


def set_roles(db: Session, user: User, roles: Iterable[str], granted_by: str) -> tuple[set[str], set[str]]:
    """把用户的系统角色设为 roles，返回 (新增, 移除)。只做规则 1～4 的检查，调用方负责 5 和审计。"""
    roles = set(roles)
    check_role_set(roles)
    current = roles_of(db, user.id)
    added, removed = roles - current, current - roles
    for code in removed:
        db.delete(db.get(UserRole, (user.id, code)))
    for code in added:
        db.add(UserRole(user_id=user.id, role_code=code, granted_by=granted_by))
    if roles & EXCLUSIVE_ROLES:  # 成为系统管理员或审计员后，不能再留在任何项目里
        for m in db.scalars(select(ProjectMember).where(ProjectMember.user_id == user.id)):
            db.delete(m)
    db.commit()
    return added, removed


# ---- 依赖 ----

def user_roles(user: User = Depends(current_user), db: Session = Depends(get_db)) -> set[str]:
    return roles_of(db, user.id)


def require_any(*allowed: str):
    """当前用户持有 allowed 中任一角色，否则 403。"""
    allowed_set = frozenset(allowed)

    def dep(roles: set[str] = Depends(user_roles)) -> set[str]:
        if not roles & allowed_set:
            need = "、".join(sorted(ROLE_NAMES.get(r, r) for r in allowed_set))
            raise HTTPException(403, f"没有权限执行此操作（需要以下角色之一：{need}）")
        return roles

    return dep


require_business = require_any(*BUSINESS_ROLES)
require_sys_admin = require_any(SYS_ADMIN)
require_auditor = require_any(SECURITY_AUDITOR)


def member_project_ids(db: Session, user_id: int) -> set[int]:
    return set(db.scalars(select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)))


def membership(db: Session, project_id: int, user_id: int) -> ProjectMember | None:
    return db.get(ProjectMember, (project_id, user_id))


def project_for_member(db: Session, code: str, user: User, *options) -> Project:
    """按代号取项目；当前用户不是成员时与“项目不存在”一样返回 404。"""
    stmt = select(Project).join(ProjectMember, ProjectMember.project_id == Project.id) \
        .where(Project.code == code, ProjectMember.user_id == user.id)
    if options:
        stmt = stmt.options(*options)
    p = db.scalar(stmt)
    if p is None:
        raise HTTPException(404, NOT_FOUND)
    return p
