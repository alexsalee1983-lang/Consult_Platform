"""角色分两层：

1. 系统角色（user_roles）：顾问、项目负责人、知识专家、知识管理员、系统管理员、安全审计员。
   系统管理员和安全审计员是“三权分立”角色，与其他任何角色互斥，且都看不到业务数据。
2. 项目内身份（project_members.project_role）：项目经理、项目成员。
   是否能看到某个项目，只看 project_members；不是成员一律 404。
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class Role(Base):
    __tablename__ = "roles"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(32))


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role_code: Mapped[str] = mapped_column(ForeignKey("roles.code"), primary_key=True)
    granted_by: Mapped[str] = mapped_column(String(64))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectMember(Base):
    __tablename__ = "project_members"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)
    project_role: Mapped[str] = mapped_column(String(16))   # manager 项目经理 / member 项目成员
    added_by: Mapped[str] = mapped_column(String(64))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
