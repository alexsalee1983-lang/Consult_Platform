"""数据表。新增表在这里导出，Alembic 通过 Base.metadata 看到全部表结构。

表结构变更只通过 backend/alembic/versions 下的迁移脚本，不再自动建表。
"""
from .agent import Agent, AgentToolGrant, Tool
from .audit import AuditLog
from .consent import ProjectConsent, SummaryCard
from .conversation import Conversation, Message
from .deliverable import DeliverableSection, SectionVersion
from .evidence import Evidence, ProjectDocument
from .finding import Finding
from .job import Job
from .judgment import JudgmentEvent
from .kb import KbChunk, KbControl, KbControlCheckpoint, KbControlMapping, KbDeidReport, KbFramework, KbGap, KbItem, KbItemAlias, KbItemLabel, KbMapping, KbPracticeDecision, KbReview, KbSource, KbSourceEvent, KbUpload, KbVersion
from .knowledge_feedback import KnowledgeFeedback
from .quality_incident import QualityIncident, QualityIncidentEvent
from .project import Confirmation, Deliverable, Project, ProjectConfig, ProjectDeletion
from .quality import QualityReview, ReviewIssue, ReviewWaiver, SignOff
from .rbac import ProjectMember, Role, UserRole
from .run import AgentRun, AgentRunStep
from .runtime_config import RuntimeConfigItem, RuntimeConfigVersion
from .usage import LlmUsage
from .user import User

__all__ = ["Agent", "AgentRun", "AgentRunStep", "AgentToolGrant", "AuditLog", "Confirmation", "Conversation", "Deliverable", "DeliverableSection", "Evidence", "Finding", "Job", "JudgmentEvent", "KbChunk", "KbControl", "KbControlCheckpoint", "KbControlMapping", "KbDeidReport", "KbFramework", "KbGap", "KbItem", "KbItemAlias", "KbItemLabel", "KbMapping", "KbPracticeDecision", "KbReview", "KbSource", "KbSourceEvent", "KbUpload", "KbVersion", "KnowledgeFeedback", "LlmUsage", "Message", "Project", "ProjectConfig", "ProjectConsent", "ProjectDeletion", "ProjectDocument", "ProjectMember", "QualityIncident", "QualityIncidentEvent", "QualityReview", "ReviewIssue", "ReviewWaiver", "Role", "RuntimeConfigItem", "RuntimeConfigVersion", "SectionVersion", "SignOff", "SummaryCard", "Tool", "User", "UserRole"]
