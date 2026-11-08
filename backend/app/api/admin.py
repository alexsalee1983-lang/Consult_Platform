"""账号与角色管理：只有系统管理员能调用；系统管理员的每个操作（含被拒绝的）都写审计。

三权分立：
- 安全审计员由单独任命，只能在服务器命令行授予或撤销，本接口一律拒绝；
- 审计员账号不能在这里修改（角色、停用、解锁都不行）；
- 系统管理员不能修改自己的角色，也不能停用自己。

建账号、重置口令、重置动态码：初始口令由平台生成，动态码密钥随机生成，只在本次响应里返回一次
（响应不缓存，审计不记），由管理员线下转交本人。初始口令登录后必须先修改（must_change_password）。
命令行 create-user 仍可用（口令由本人当场输入，不要求修改）。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit, sessions
from ..services import jobs, usage
from ..core.auth import current_user
from ..core.db import get_db, utcnow
from ..core.permissions import (
    ROLE_NAMES,
    SECURITY_AUDITOR,
    RoleRuleError,
    check_role_set,
    require_sys_admin,
    roles_of,
    set_roles,
)
from ..core.security import generate_initial_password, hash_password, new_totp_secret, totp_uri
from ..models import User

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_sys_admin)])


class UserOut(BaseModel):
    username: str
    display_name: str
    is_active: bool
    locked: bool
    roles: list[str]
    must_change_password: bool
    last_login_at: datetime | None
    created_at: datetime | None


class RolesIn(BaseModel):
    roles: list[str]


USERNAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]{1,63}$"


class CreateUserIn(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    display_name: str = Field(default="", max_length=64)
    roles: list[str] = []


class Credentials(BaseModel):
    """只返回一次的初始凭据；没有的项为空。"""
    user: UserOut
    initial_password: str = ""
    totp_secret: str = ""
    totp_uri: str = ""


def _out(db: Session, u: User) -> UserOut:
    lu = u.locked_until
    locked = bool(lu and (lu if lu.tzinfo else lu.replace(tzinfo=utcnow().tzinfo)) > utcnow())
    return UserOut(username=u.username, display_name=u.display_name or u.username, is_active=u.is_active,
                   locked=locked, roles=sorted(roles_of(db, u.id)), must_change_password=u.must_change_password,
                   last_login_at=u.last_login_at, created_at=u.created_at)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


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


@router.get("/agent-grants")
def agent_grants(db: Session = Depends(get_db)) -> list[dict]:
    """Agent × 工具授权矩阵（只读；改授权走迁移和代码审阅），附 md 就绪情况和 md 与授权表的差异提示。"""
    from ..agents import runtime
    from ..models import Agent

    out = []
    for a in db.scalars(select(Agent).order_by(Agent.side, Agent.code)):
        i = runtime.agent_info(db, a)
        out.append({"agent": a.code, "name": a.name, "side": a.side, "phase": a.phase, "enabled": a.enabled,
                    "tools": i.tools, "ready": i.ready, "reason": i.reason, "prompt_version": i.prompt_version,
                    "md_notes": i.notes})
    return out


@router.get("/usage")
def usage_report(days: int = Query(30, ge=1, le=366), db: Session = Depends(get_db)) -> list[dict]:
    """大模型用量，按人、模型汇总。三权分立：系统管理员看不到项目维度（项目维度用命令行 usage-report）。"""
    return usage.summary(db, days, ("username", "model"))


class JobOut(BaseModel):
    """不含任务参数和项目代号：系统管理员看不到业务数据，只看运行状态。"""
    id: int
    kind: str
    status: str
    attempts: int
    max_attempts: int
    last_error: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@router.get("/jobs")
def list_jobs(db: Session = Depends(get_db)) -> dict:
    from ..models import Job

    recent = db.scalars(select(Job).order_by(Job.id.desc()).limit(50))
    return {"summary": jobs.summary(db),
            "recent": [JobOut.model_validate(j, from_attributes=True).model_dump(mode="json") for j in recent]}


@router.post("/jobs/selfcheck", status_code=202)
def enqueue_selfcheck(db: Session = Depends(get_db), admin: User = Depends(current_user)) -> dict:
    """排一个 worker 自检任务（连数据库、在项目区写读删一个测试对象），用于部署后确认后台任务链路。"""
    job_id = jobs.enqueue(db, "system.selfcheck", created_by=admin.username, max_attempts=1)
    audit.record(db, actor=admin.username, action="job.enqueue", result="allowed", job_id=job_id,
                 kind="system.selfcheck")
    return {"job_id": job_id}


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry_job(job_id: int, db: Session = Depends(get_db), admin: User = Depends(current_user)):
    job = jobs.retry(db, job_id)
    if job is None:
        raise HTTPException(404, "任务不存在或不是失败状态")
    audit.record(db, actor=admin.username, action="job.retry", result="allowed", job_id=job_id, kind=job.kind)
    return JobOut.model_validate(job, from_attributes=True)


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)):
    return [_out(db, u) for u in db.scalars(select(User).order_by(User.username))]


@router.post("/users", response_model=Credentials, status_code=201)
def create_user(body: CreateUserIn, response: Response, db: Session = Depends(get_db),
                admin: User = Depends(current_user)):
    if SECURITY_AUDITOR in body.roles:
        _deny(db, admin, "user.create", body.username, "grant_auditor", 403,
              "安全审计员由单独任命，只能在服务器命令行授予")
    try:
        check_role_set(body.roles)
    except RoleRuleError as e:
        _deny(db, admin, "user.create", body.username, "role_rule", 422, str(e))
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, "用户名已存在")
    password, secret = generate_initial_password(), new_totp_secret()
    user = User(username=body.username, display_name=body.display_name.strip() or body.username,
                password_hash=hash_password(password), totp_secret=secret, must_change_password=True)
    db.add(user)
    db.commit()
    set_roles(db, user, body.roles, granted_by=admin.username)
    audit.record(db, actor=admin.username, action="user.create", result="allowed", target=body.username,
                 roles=sorted(body.roles))
    _no_store(response)
    return Credentials(user=_out(db, user), initial_password=password, totp_secret=secret,
                       totp_uri=totp_uri(secret, user.username))


@router.post("/users/{username}/reset-password", response_model=Credentials)
def reset_password(username: str, response: Response, db: Session = Depends(get_db),
                   admin: User = Depends(current_user)):
    """生成新的初始口令（同时解除锁定），结束该用户全部会话；下次登录须先改口令。"""
    target = _target(db, admin, username, "user.reset_password")
    password = generate_initial_password()
    target.password_hash = hash_password(password)
    target.must_change_password = True
    target.failed_logins = 0
    target.locked_until = None
    db.commit()
    sessions.destroy_all_for_user(target.id)
    audit.record(db, actor=admin.username, action="user.reset_password", result="allowed", target=username)
    _no_store(response)
    return Credentials(user=_out(db, target), initial_password=password)


@router.post("/users/{username}/reset-totp", response_model=Credentials)
def reset_totp(username: str, response: Response, db: Session = Depends(get_db),
               admin: User = Depends(current_user)):
    """用户换手机或丢失动态码时：生成新的动态码密钥，旧的立即失效，结束该用户全部会话。"""
    target = _target(db, admin, username, "user.reset_totp")
    secret = new_totp_secret()
    target.totp_secret = secret
    target.totp_last_step = 0
    db.commit()
    sessions.destroy_all_for_user(target.id)
    audit.record(db, actor=admin.username, action="user.reset_totp", result="allowed", target=username)
    _no_store(response)
    return Credentials(user=_out(db, target), totp_secret=secret, totp_uri=totp_uri(secret, target.username))


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
