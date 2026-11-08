"""数据表。新增表在这里导出，Alembic 通过 Base.metadata 看到全部表结构。

表结构变更只通过 backend/alembic/versions 下的迁移脚本，不再自动建表。
"""
from .agent import Agent, AgentToolGrant, Tool
from .audit import AuditLog
from .project import Confirmation, Deliverable, Project
from .rbac import ProjectMember, Role, UserRole
from .usage import LlmUsage
from .user import User

__all__ = ["Agent", "AgentToolGrant", "Tool", "AuditLog", "Confirmation", "Deliverable", "LlmUsage", "Project", "ProjectMember", "Role", "User",
           "UserRole"]
