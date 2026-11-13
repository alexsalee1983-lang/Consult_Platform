"""严格档客户同意记录与去标识摘要卡（M4 ④，迁移 0027）。

- ProjectConsent：客户书面同意在离场后使用去标识摘要卡（《数据处理计划》第四节勾选“同意”）。只记文号和签署日期，
  不写客户名称；同一项目同时只有一条有效记录。客户书面撤回后标记撤回，已上传的摘要卡内容一并清空。
- SummaryCard：去标识摘要卡，编号 S-01 起按项目递增、不复用。只写控制项层面的事实：评估项、现状、数量级、
  现场证据编号（证据原文留在现场）。上传时过机密级检测和摘要卡规则检查（真名、原话、IP、系统名）。
"""
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import false as sql_false

from ..core.db import Base, utcnow


class ProjectConsent(Base):
    __tablename__ = "project_consents"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    doc_ref: Mapped[str] = mapped_column(String(64))              # 客户书面同意的文号 / 编号，不写客户名称
    signed_on: Mapped[date] = mapped_column(Date)
    recorded_by: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked: Mapped[bool] = mapped_column(Boolean, server_default=sql_false())
    revoke_ref: Mapped[str] = mapped_column(String(64), server_default="")
    revoked_on: Mapped[date | None] = mapped_column(Date, nullable=True)     # 收到客户书面撤回的日期
    revoked_by: Mapped[str] = mapped_column(String(64), server_default="")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SummaryCard(Base):
    __tablename__ = "summary_cards"
    __table_args__ = (UniqueConstraint("project_id", "code", name="uq_summary_cards_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    consent_id: Mapped[int] = mapped_column(ForeignKey("project_consents.id"), index=True)
    code: Mapped[str] = mapped_column(String(16))                 # S-01
    seq: Mapped[int] = mapped_column(Integer)
    item: Mapped[str] = mapped_column(String(200))                # 评估项
    finding: Mapped[str] = mapped_column(Text, default="")        # 现状（控制项层面的事实）
    magnitude: Mapped[str] = mapped_column(String(100), default="")   # 数量级，如“约 30 个”“近 12 个月”
    onsite_refs: Mapped[list] = mapped_column(JSON, default=list)     # 现场证据编号，原文留在现场
    client_reviewed_on: Mapped[date] = mapped_column(Date)       # 客户对接人审阅签字日期
    sha256: Mapped[str] = mapped_column(String(64))               # 评估项 + 现状的摘要，同一项目不重复上传
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    withdrawn: Mapped[bool] = mapped_column(Boolean, server_default=sql_false())   # 撤下或同意撤回：内容已清空
    withdrawn_by: Mapped[str] = mapped_column(String(64), server_default="")
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    consent: Mapped[ProjectConsent] = relationship()
