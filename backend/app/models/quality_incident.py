"""AI 质量事件台账（四期 F3，迁移 0051；4A 架构设计 6.3）。

治理审计数据：落主库、两张表都只追加（库级触发器拒绝改、删），保留至少 1 年；不随项目删除
（只存项目代号字符串，不建外键）。不存任何原文：描述和处置说明只记长度和摘要哈希。
规则只在 app/services/quality_incidents.py。
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class QualityIncident(Base):
    """一条质量事件的登记（不修改；级别、状态由处置记录推出）。"""
    __tablename__ = "quality_incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_code: Mapped[str] = mapped_column(String(32), default="", index=True)   # 空 = 不属项目（如知识条目）
    category: Mapped[str] = mapped_column(String(32))          # 见 services/quality_incidents.CATEGORIES
    ref_kind: Mapped[str] = mapped_column(String(16))          # deliverable / kb_item / run
    ref_id: Mapped[str] = mapped_column(String(64))
    description_len: Mapped[int] = mapped_column(Integer, default=0)
    description_sha: Mapped[str] = mapped_column(String(16), default="")
    reported_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class QualityIncidentEvent(Base):
    """八步处置的每一步（只追加）：register 登记 grade 分级 contain 遏制 assess 影响评估 correct 更正与沟通
    root_cause 根因 prevent 纠正预防 review 复盘（复盘即关闭）。"""
    __tablename__ = "quality_incident_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("quality_incidents.id"), index=True)
    step: Mapped[str] = mapped_column(String(16))
    level: Mapped[str] = mapped_column(String(4), default="")          # grade：P1 / P2 / P3
    action: Mapped[str] = mapped_column(String(24), default="")        # contain：遏制措施
    root_cause: Mapped[str] = mapped_column(String(16), default="")    # root_cause：根因类别
    note_len: Mapped[int] = mapped_column(Integer, default=0)
    note_sha: Mapped[str] = mapped_column(String(16), default="")
    actor: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
