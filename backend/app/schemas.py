from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

PHASES = ["启动", "调研", "分析", "设计", "汇报", "验收"]


class DeliverableOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    title: str
    status: str


class ConfirmationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    title: str
    agent: str
    status: str
    project_code: str = ""
    agent_code: str = ""
    options: list[dict] = []          # [{"key": "A", "text": "……"}]
    recommendation: str = ""
    reason: str = ""
    impact: str = ""
    deliverable_code: str = ""
    section_seq: int | None = None
    created_by: str = ""
    created_at: datetime | None = None
    choice: str = ""
    resolution: str = ""
    resolved_by: str = ""
    resolved_at: datetime | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    service: str
    industry: str
    tier: str
    phase: int
    phase_name: str = ""
    week: int
    total_weeks: int
    posture_score: int | None
    next_milestone: str
    open_confirmations: int = 0
    # 保留与删除（M4 ①）：retention_state = active 未验收 / retained / due_soon 到期前 30 天内 / due 已到期
    accepted_on: date | None = None
    retention_months: int = 6
    deletion_due_on: date | None = None
    retention_state: str = "active"
    days_to_deletion: int | None = None
    flowback_allowed: bool = True           # False：客户不同意回流（M4 ②）


class ProjectDetail(ProjectOut):
    deliverables: list[DeliverableOut]
    confirmations: list[ConfirmationOut]


class ResolveIn(BaseModel):
    """选一个选项（choice），或写意见（resolution），或两者都有。"""
    choice: str = Field(default="", max_length=1)
    resolution: str = Field(default="", max_length=2000)


class GuardIn(BaseModel):
    text: str = Field(max_length=200_000)


class FindingOut(BaseModel):
    kind: str
    label: str
    excerpt: str


class GuardOut(BaseModel):
    blocked: bool
    summary: str
    findings: list[FindingOut]


class ChatIn(BaseModel):
    project_code: str
    message: str = Field(min_length=1, max_length=20_000)
    level: str = Field(default="standard", pattern="^(reasoning|standard|light)$")


class ChatOut(BaseModel):
    reply: str
    model: str
    demo: bool


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    hash: str | None = None   # 为空表示哈希链启用前的旧记录
    at: datetime
    actor: str
    project_code: str
    action: str
    result: str
    detail: str
