"""判断数据埋点（4A 架构设计 v4.0 §7.2，迁移 0038）：顾问对 Agent 产出的判断，用来改进 Agent 和沉淀公司经验。

只记计数和类别，不记正文、标题、选项文字和意见（决策记录 2026-11-18：内部信息等法务意见，采集开关默认关）。
kind：edit 顾问改了 Agent 写的章节（magnitude = 改动幅度 0～1，category 为 light / moderate / heavy）；
card 顾问处理了确认卡（category 为卡片类别，attrs 记选项数、是否选了、是否按推荐、是否写了意见）。
规则只在 app/project/judgment.py；项目删除时一并删除。
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class JudgmentEvent(Base):
    __tablename__ = "judgment_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))                       # edit / card
    deliverable_code: Mapped[str] = mapped_column(String(8), default="")
    section_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_code: Mapped[str] = mapped_column(String(32), default="")
    category: Mapped[str] = mapped_column(String(32), default="")
    magnitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    attrs: Mapped[dict] = mapped_column(JSON, default=dict)             # 只放数字和布尔
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
