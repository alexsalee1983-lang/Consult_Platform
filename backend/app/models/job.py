from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class Job(Base):
    """后台任务（解析、向量化、到期删除等），由 worker 容器执行。见 app/services/jobs.py。

    数据库是任务的唯一依据（Redis 只用来唤醒 worker）：worker 崩溃或重启不会丢任务，超时的任务会被重新领取。
    payload 只放编号、对象键这类引用，不放客户原文。
    """

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_run_after", "status", "run_after"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")              # JSON，只放引用，最多 4KB
    status: Mapped[str] = mapped_column(String(16), default="queued")      # queued / running / done / failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker: Mapped[str] = mapped_column(String(64), default="")
    last_error: Mapped[str] = mapped_column(String(200), default="")      # 只记错误类型和处理器给出的安全说明
    project_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
