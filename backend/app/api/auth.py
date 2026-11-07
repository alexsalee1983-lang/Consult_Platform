from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit, sessions
from ..core.auth import current_user
from ..core.config import get_settings
from ..core.db import get_db, utcnow
from ..core.security import match_totp_step, verify_password
from ..models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])

LOGIN_FAILED = "用户名、口令或动态码错误"
LOCKED = "登录失败次数过多，账号已临时锁定，请稍后再试或联系系统管理员"


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    totp: str = Field(min_length=6, max_length=6)


class MeOut(BaseModel):
    username: str
    display_name: str
    csrf_token: str


def _set_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        sessions.COOKIE_NAME, token, httponly=True, secure=s.cookie_secure, samesite="strict", path="/",
        max_age=s.session_absolute_hours * 3600,
    )


def _me(user: User, csrf: str) -> MeOut:
    return MeOut(username=user.username, display_name=user.display_name or user.username, csrf_token=csrf)


def _as_aware(dt):
    # SQLite 读回来的时间不带时区；PostgreSQL 带。统一按 UTC 处理。
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=utcnow().tzinfo)


@router.post("/login", response_model=MeOut)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    s = get_settings()
    user = db.scalar(select(User).where(User.username == body.username))

    # 用户不存在时也做一次口令校验，响应时间与用户存在时一致
    password_ok = verify_password(user.password_hash if user else None, body.password)
    if user is None or not user.is_active:
        # 不记输入的用户名原文：用户有时会把口令误填进用户名框
        audit.record(db, actor="(unknown)", action="auth.login", result="blocked", reason="unknown_or_inactive",
                     username_sha=audit.digest(body.username))
        raise HTTPException(401, LOGIN_FAILED)

    now = utcnow()
    locked_until = _as_aware(user.locked_until)
    if locked_until and locked_until > now:
        audit.record(db, actor=user.username, action="auth.login", result="blocked", reason="locked")
        raise HTTPException(401, LOCKED)

    step = match_totp_step(user.totp_secret, body.totp) if password_ok else None
    replay = step is not None and step <= user.totp_last_step
    if not password_ok or step is None or replay:
        reason = "bad_password" if not password_ok else ("totp_replay" if replay else "bad_totp")
        user.failed_logins += 1
        if user.failed_logins >= s.login_max_failures:
            user.locked_until = now + timedelta(minutes=s.login_lock_minutes)
            user.failed_logins = 0
            reason += "+locked"
        db.commit()
        audit.record(db, actor=user.username, action="auth.login", result="blocked", reason=reason)
        raise HTTPException(401, LOGIN_FAILED)

    user.failed_logins = 0
    user.locked_until = None
    user.totp_last_step = step
    user.last_login_at = now
    db.commit()

    # 登录成功换新的会话 ID：作废请求里带来的旧会话
    sessions.destroy(request.cookies.get(sessions.COOKIE_NAME))
    token, data = sessions.create(user.id, user.username)
    _set_cookie(response, token)
    audit.record(db, actor=user.username, action="auth.login", result="allowed")
    return _me(user, data.csrf)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db),
           user: User = Depends(current_user)):
    sessions.destroy(request.cookies.get(sessions.COOKIE_NAME))
    response.delete_cookie(sessions.COOKIE_NAME, path="/", httponly=True, secure=get_settings().cookie_secure,
                           samesite="strict")
    audit.record(db, actor=user.username, action="auth.logout", result="allowed")


@router.get("/me", response_model=MeOut)
def me(request: Request, user: User = Depends(current_user)):
    return _me(user, request.state.session.csrf)
