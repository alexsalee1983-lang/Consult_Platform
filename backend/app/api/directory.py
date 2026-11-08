"""用户目录：项目经理添加成员时按用户名或姓名查找同事。

只返回启用中、持业务角色的账号（系统管理员、安全审计员不能加入项目，不出现在这里），
只给用户名和姓名，不含角色、登录时间等信息。要求业务角色。
"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..core.db import get_db
from ..core.permissions import BUSINESS_ROLES, require_business
from ..models import User, UserRole

router = APIRouter(prefix="/api", tags=["directory"], dependencies=[Depends(require_business)])

LIMIT = 20


class DirectoryUser(BaseModel):
    username: str
    display_name: str


@router.get("/users/lookup", response_model=list[DirectoryUser])
def lookup(q: str = Query(min_length=1, max_length=64), db: Session = Depends(get_db)):
    # 用户输入里的 % 和 _ 按普通字符匹配
    like = "%" + q.strip().replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
    business = select(UserRole.user_id).where(UserRole.role_code.in_(BUSINESS_ROLES))
    rows = db.scalars(select(User).where(User.is_active.is_(True), User.id.in_(business),
                                         or_(User.username.ilike(like, escape="\\"),
                                             User.display_name.ilike(like, escape="\\")))
                      .order_by(User.username).limit(LIMIT))
    return [DirectoryUser(username=u.username, display_name=u.display_name or u.username) for u in rows]
