"""Agent md 版本库与撤销回滚接口（四期 F3②e）。规则在 app/services/agent_md.py。

- GET  /api/agent-md/{代号}/versions：版本历史、当前版本、回滚指针（项目负责人、系统管理员）；
- GET  /api/agent-md/{代号}/versions/{版本}：该版本的公共规范与角色 md 原文（同上；md 不含客户信息）；
- POST /api/agent-md/{代号}/unpin：撤销回滚，恢复仓库当前版本（同上；理由只记长度）。
回滚本身经质量事件遏制“回滚 Agent 版本”（POST /api/quality-incidents/{id}/events）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import PROJECT_LEAD, SYS_ADMIN, require_any
from ..models import User
from ..services import agent_md as svc

router = APIRouter(prefix="/api/agent-md", tags=["agent-md"],
                   dependencies=[Depends(require_any(PROJECT_LEAD, SYS_ADMIN))])


class UnpinIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


def _err(e: svc.MdError) -> HTTPException:
    return HTTPException(e.status, str(e))


@router.get("/{code}/versions")
def versions(code: str, db: Session = Depends(get_db)) -> dict:
    try:
        return svc.history(db, code)
    except svc.MdError as e:
        raise _err(e) from None


@router.get("/{code}/versions/{version}")
def version_content(code: str, version: str, db: Session = Depends(get_db)) -> dict:
    try:
        return svc.content(db, code, version)
    except svc.MdError as e:
        raise _err(e) from None


@router.post("/{code}/unpin")
def unpin(code: str, body: UnpinIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    try:
        return svc.unpin(db, user.username, code, body.reason)
    except svc.MdError as e:
        raise _err(e) from None
