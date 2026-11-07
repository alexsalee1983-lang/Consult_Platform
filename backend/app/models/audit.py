from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class AuditLog(Base):
    """审计日志。只记元数据（长度、摘要哈希、命中类别），不记客户原文。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    project_code: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(32))
    result: Mapped[str] = mapped_column(String(16))   # allowed / blocked / error
    detail: Mapped[str] = mapped_column(Text, default="")
