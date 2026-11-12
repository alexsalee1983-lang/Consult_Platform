"""质量门禁与签署（M3 ⑤，迁移 0021）。规则见 app/project/quality.py。

- QualityReview：一次质量评审（质量评审与异议应答 Agent + 平台硬规则），针对交付件的某个修订（revision），
  记下评审时各章的版本号。问题清单和评审报告属于项目区内容，只有项目成员能看，不进审计和日志。
- SignOff：签署流程的每一个动作（只追加）：submit 顾问审核完成、提交签署；sign 项目经理签署；
  return 项目经理退回修改。记下动作时的修订号、各章版本号和依据的质量评审。
- ReviewWaiver（迁移 0022）：项目经理驳回某次评审中质量评审 Agent 提出的某条阻断问题（只追加，必须写理由）。
  平台硬规则判出的阻断不能驳回。理由属于项目区内容，不进审计。
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class QualityReview(Base):
    __tablename__ = "quality_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    deliverable_id: Mapped[int] = mapped_column(ForeignKey("deliverables.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)                     # 评审的交付件修订号
    versions: Mapped[dict] = mapped_column(JSON, default=dict)         # {"章节号": 版本号}
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued / running / done / failed
    conclusion: Mapped[str] = mapped_column(String(16), default="")    # pass 通过 / revise 修改后通过 / fail 不通过
    blocking: Mapped[int] = mapped_column(Integer, default=0)          # 阻断
    major: Mapped[int] = mapped_column(Integer, default=0)             # 重要
    minor: Mapped[int] = mapped_column(Integer, default=0)             # 建议
    issues: Mapped[list] = mapped_column(JSON, default=list)           # [{severity, location, dimension, problem, suggestion, by}]
    report: Mapped[str] = mapped_column(Text, default="")              # Agent 评审意见全文（含挑战预判）
    error: Mapped[str] = mapped_column(String(200), default="")        # 失败原因（只写类别）
    agent_code: Mapped[str] = mapped_column(String(32), default="")
    prompt_version: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SignOff(Base):
    __tablename__ = "sign_offs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    deliverable_id: Mapped[int] = mapped_column(ForeignKey("deliverables.id"), index=True)
    action: Mapped[str] = mapped_column(String(16))                    # submit / sign / return
    revision: Mapped[int] = mapped_column(Integer)
    versions: Mapped[dict] = mapped_column(JSON, default=dict)         # {"章节号": 版本号}
    review_id: Mapped[int | None] = mapped_column(ForeignKey("quality_reviews.id"), nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")             # 审核意见 / 退回理由（项目区内容，不进审计）
    actor: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewWaiver(Base):
    __tablename__ = "review_waivers"
    __table_args__ = (UniqueConstraint("review_id", "issue_index", name="uq_review_waivers_issue"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("quality_reviews.id"), index=True)
    issue_index: Mapped[int] = mapped_column(Integer)                  # 问题清单中的下标（从 0 开始）
    reason: Mapped[str] = mapped_column(Text)                          # 驳回理由（项目区内容，不进审计）
    actor: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
