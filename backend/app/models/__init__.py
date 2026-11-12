"""数据表。新增表在这里导出，Alembic 通过 Base.metadata 看到全部表结构。

表结构变更只通过 backend/alembic/versions 下的迁移脚本，不再自动建表。
"""
from .agent import Agent, AgentToolGrant, Tool
from .audit import AuditLog
from .conversation import Conversation, Message
from .deliverable import DeliverableSection, SectionVersion
from .evidence import Evidence, ProjectDocument
from .job import Job
from .kb import KbChunk, KbDeidReport, KbItem, KbReview, KbUpload, KbVersion
from .project import Confirmation, Deliverable, Project
from .quality import QualityReview, SignOff
from .rbac import ProjectMember, Role, UserRole
from .run import AgentRun, AgentRunStep
from .usage import LlmUsage
from .user import User

__all__ = ["Agent", "AgentRun", "AgentRunStep", "AgentToolGrant", "AuditLog", "Confirmation", "Conversation", "Deliverable", "DeliverableSection", "Evidence", "Job", "KbChunk", "KbDeidReport", "KbItem", "KbReview",
           "KbUpload", "KbVersion", "LlmUsage",
           "Message", "Project", "ProjectDocument", "ProjectMember", "QualityReview", "Role", "SectionVersion", "SignOff", "Tool", "User", "UserRole"]
