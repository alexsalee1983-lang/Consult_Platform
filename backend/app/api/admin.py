"""账号与角色管理：只有系统管理员能调用；系统管理员的每个操作（含被拒绝的）都写审计。

三权分立：
- 安全审计员由单独任命，只能在服务器命令行授予或撤销，本接口一律拒绝；
- 审计员账号不能在这里修改（角色、停用、解锁都不行）；
- 系统管理员不能修改自己的角色，也不能停用自己。
用户创建暂用命令行（python -m app.cli create-user），用户管理界面在 S1 后续 PR。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit, sessions
from ..core.auth import current_user
from ..core.db import get_db, utcnow
from ..core.permissions import ROLE_NAMES, SECURITY_AUDITOR, RoleRuleError, require_sys_admin, roles_of, set_roles
from ..models import User

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_sys_admin)])


class UserOut(BaseModel):
    username: str
    display_name: str
    is_active: bool
    locked: bool
    roles: list[str]
    last_login_at: datetime | None


class RolesIn(BaseModel):
    roles: list[str]


def _out(db: Session, u: User) -> UserOut:
    lu = u.locked_until
    locked = bool(lu and (lu if lu.tzinfo else lu.replace(tzinfo=utcnow().tzinfo)) > utcnow())
    return UserOut(username=u.username, display_name=u.display_name or u.username, is_active=u.is_active,
                   locked=locked, roles=sorted(roles_of(db, u.id)), last_login_at=u.last_login_at)


def _deny(db: Session, admin: User, action: str, target: str, reason: str, status: int, message: str):
    audit.record(db, actor=admin.username, action=action, result="blocked", target=target, reason=reason)
    raise HTTPException(status, message)


def _target(db: Session, admin: User, username: str, action: str) -> User:
    """取被操作的账号，并执行三权分立的通用限制。"""
    target = db.scalar(select(User).where(User.username == username))
    if target is None:
        raise HTTPException(404, "用户不存在")
    if target.id == admin.id:
        _deny(db, admin, action, username, "self", 403, "不能对自己的账号执行此操作")
    if SECURITY_AUDITOR in roles_of(db, target.id):
        _deny(db, admin, action, username, "auditor_account", 403, "安全审计员账号只能在服务器命令行管理")
    return target


@router.get("/roles")
def list_roles() -> dict[str, str]:
    return ROLE_NAMES


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)):
    return [_out(db, u) for u in db.scalars(select(User).order_by(User.username))]


@router.put("/users/{username}/roles", response_model=UserOut)
def put_roles(username: str, body: RolesIn, db: Session = Depends(get_db), admin: User = Depends(current_user)):
    target = _target(db, admin, username, "user.roles")
    if SECURITY_AUDITOR in body.roles:
        _deny(db, admin, "user.roles", username, "grant_auditor", 403,
              "安全审计员由单独任命，只能在服务器命令行授予")
    try:
        added, removed = set_roles(db, target, body.roles, granted_by=admin.username)
    except RoleRuleError as e:
        _deny(db, admin, "user.roles", username, "role_rule", 422, str(e))
    audit.record(db, actor=admin.username, action="user.roles", result="allowed", target=username,
                 added=sorted(added), removed=sorted(removed))
    return _out(db, target)


def _set_active(db: Session, admin: User, username: str, active: bool) -> UserOut:
    action = "user.enable" if active else "user.disable"
    target = _target(db, admin, username, action)
    target.is_active = active
    db.commit()
    if not active:
        sessions.destroy_all_for_user(target.id)
    audit.record(db, actor=admin.username, action=action, result="allowed", target=username)
    return _out(db, target)


@router.post("/users/{username}/disable", response_model=UserOut)
def disable(username: str, db: Session = Depends(get_db), admin: User = Depends(current_user)):
    return _set_active(db, admin, username, False)


@router.post("/users/{username}/enable", response_model=UserOut)
def enable(username: str, db: Session = Depends(get_db), admin: User = Depends(current_user)):
    return _set_active(db, admin, username, True)


@router.post("/users/{username}/unlock", response_model=UserOut)
def unlock(username: str, db: Session = Depends(get_db), admin: User = Depends(current_user)):
    target = _target(db, admin, username, "user.unlock")
    target.failed_logins = 0
    target.locked_until = None
    db.commit()
    audit.record(db, actor=admin.username, action="user.unlock", result="allowed", target=username)
    return _out(db, target)
