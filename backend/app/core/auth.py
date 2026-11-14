"""登录检查：默认所有接口都要登录，只有 PUBLIC_PATHS 例外。

作为应用级依赖挂在 FastAPI 上（见 main.py），新加的接口自动受保护，不需要逐个声明。
修改类请求（POST / PUT / PATCH / DELETE）还要校验 CSRF 令牌。
管理员建的账号或重置过口令的账号（must_change_password）、口令超过有效期的账号（PASSWORD_MAX_AGE_DAYS，等保自查 ②d），
改口令前只能访问 PASSWORD_CHANGE_PATHS。
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..models import User
from . import sessions
from .config import get_settings
from .db import get_db, utcnow

# 不需要登录的接口：健康检查（Docker 用，只返回组件状态）和登录本身
PUBLIC_PATHS = frozenset({"/api/health", "/api/auth/login"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# 须先改口令的账号只能访问这些接口
PASSWORD_CHANGE_PATHS = frozenset({"/api/auth/me", "/api/auth/logout", "/api/auth/password"})
MUST_CHANGE_PASSWORD = "请先修改初始口令，再使用其他功能"
PASSWORD_EXPIRED = "口令已超过有效期，请先修改口令，再使用其他功能"


def password_expired(user: User) -> bool:
    days = get_settings().password_max_age_days
    at = user.password_changed_at
    if days <= 0 or at is None:
        return False
    at = at if at.tzinfo else at.replace(tzinfo=utcnow().tzinfo)  # SQLite 读回不带时区
    return utcnow() - at > timedelta(days=days)


def password_change_required(user: User) -> bool:
    return user.must_change_password or password_expired(user)

NOT_LOGGED_IN = "未登录或会话已过期，请重新登录"


def authenticate(request: Request, db: Session = Depends(get_db)) -> None:
    if request.url.path in PUBLIC_PATHS:
        return
    token = request.cookies.get(sessions.COOKIE_NAME)
    data = sessions.load(token)
    if data is None:
        raise HTTPException(401, NOT_LOGGED_IN)
    user = db.get(User, data.user_id)
    if user is None or not user.is_active:
        sessions.destroy(token)
        raise HTTPException(401, NOT_LOGGED_IN)
    if request.method in UNSAFE_METHODS and not sessions.csrf_ok(data, request.headers.get(sessions.CSRF_HEADER)):
        raise HTTPException(403, "请求缺少有效的 CSRF 令牌，请刷新页面后重试")
    if request.url.path not in PASSWORD_CHANGE_PATHS:
        if user.must_change_password:
            raise HTTPException(403, MUST_CHANGE_PASSWORD)
        if password_expired(user):
            raise HTTPException(403, PASSWORD_EXPIRED)
    request.state.user = user
    request.state.session = data


def current_user(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if user is None:  # 只会出现在公开接口上误用本依赖
        raise HTTPException(401, NOT_LOGGED_IN)
    return user
