from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class AuditLog(Base):
    """审计日志。只记元数据（长度、摘要哈希、命中类别），不记客户原文。

    只追加：每条记录含上一条的哈希（prev_hash）和本条的哈希（hash），组成哈希链，
    任何一条被改、被删或被插入都能由 /api/audit/verify 检出。数据库层用触发器禁止 UPDATE / DELETE。
    hash 为空的是哈希链启用前（M0 和登录功能上线初期）的旧记录，从创世记录（action=audit.genesis）起进入链。
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    project_code: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(32))
    result: Mapped[str] = mapped_column(String(16))   # allowed / blocked / error
    detail: Mapped[str] = mapped_column(Text, default="")
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
