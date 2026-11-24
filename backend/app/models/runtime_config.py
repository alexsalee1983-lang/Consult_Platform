"""运行层配置改动（四期 G3②，docs/4A架构设计.md 5.6 配置三层，迁移 0050）。

一次改动是一个版本（runtime_config_versions），含一到多项键值（runtime_config_items）；只追加，不改不删，
状态只从 pending 走到 applied / rejected。生效配置 = 按版本号顺序叠加全部 applied 版本，未覆盖的键取 .env。
value 为 None 表示恢复 .env 默认值。规则只在 app/services/runtime_config.py。
"""
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class RuntimeConfigVersion(Base):
    __tablename__ = "runtime_config_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    reason: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), index=True)          # pending / applied / rejected
    needs_confirm: Mapped[bool] = mapped_column(Boolean, default=False)
    rollback_to: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 回滚到的版本号（0 = 全部恢复 .env）
    requested_by: Mapped[str] = mapped_column(String(64))
    decided_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RuntimeConfigItem(Base):
    __tablename__ = "runtime_config_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("runtime_config_versions.id"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[Any] = mapped_column(JSON, nullable=True)              # None = 恢复 .env 默认值
    old_value: Mapped[Any] = mapped_column(JSON, nullable=True)          # 提交时的生效值，便于审阅
