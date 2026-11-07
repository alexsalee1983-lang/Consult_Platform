from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # 客户代号，如 BANK-A
    service: Mapped[str] = mapped_column(String(64))          # 服务产品
    industry: Mapped[str] = mapped_column(String(32))
    tier: Mapped[str] = mapped_column(String(16), default="standard")  # standard 标准档 / strict 严格档
    phase: Mapped[int] = mapped_column(Integer, default=0)   # 0 启动 … 5 验收
    week: Mapped[int] = mapped_column(Integer, default=1)
    total_weeks: Mapped[int] = mapped_column(Integer, default=12)
    posture_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_milestone: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    deliverables: Mapped[list["Deliverable"]] = relationship(
        back_populates="project", order_by="Deliverable.code", cascade="all, delete-orphan"
    )
    confirmations: Mapped[list["Confirmation"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Deliverable(Base):
    __tablename__ = "deliverables"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    code: Mapped[str] = mapped_column(String(8))      # D1 … D8
    title: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="not_started")
    # not_started 未开始 / drafting 起草中 / review 待审核 / signed 已签署

    project: Mapped[Project] = relationship(back_populates="deliverables")


class Confirmation(Base):
    """需要顾问做判断的事项：方案取舍、来源冲突、证据不足、入库确认、范围确认。"""

    __tablename__ = "confirmations"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(256))
    agent: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="open")  # open / resolved
    resolution: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped[Project] = relationship(back_populates="confirmations")


class AuditLog(Base):
    """审计日志。只记元数据（长度、摘要哈希、命中类别），不记客户原文。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    project_code: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(32))
    result: Mapped[str] = mapped_column(String(16))   # allowed / blocked / error
    detail: Mapped[str] = mapped_column(Text, default="")
