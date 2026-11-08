"""登录检查：默认所有接口都要登录，只有 PUBLIC_PATHS 例外。

作为应用级依赖挂在 FastAPI 上（见 main.py），新加的接口自动受保护，不需要逐个声明。
修改类请求（POST / PUT / PATCH / DELETE）还要校验 CSRF 令牌。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..models import User
from . import sessions
from .db import get_db

# 不需要登录的接口：健康检查（Docker 用，只返回组件状态）和登录本身
PUBLIC_PATHS = frozenset({"/api/health", "/api/auth/login"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

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
    request.state.user = user
    request.state.session = data


def current_user(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if user is None:  # 只会出现在公开接口上误用本依赖
        raise HTTPException(401, NOT_LOGGED_IN)
    return user
