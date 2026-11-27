"""四期 F4②：改稿与驳回特征表（迁移 0054），给 F4③ 触发引擎提供信号。规则只在 app/project/revision_features.py。

每行一条特征，不存正文和标题：只存章节键（交付件代号.章节序号，如 D4.1）、来源、差异类别、计数、
改动幅度均值（0～1）和摘录哈希（16 位）。随项目删除一并删除（app/project/deletion.py）。
source：revision 改稿 / review_waiver 评审驳回（practice_reject 做法确认驳回在知识区，只读汇总，不落本表）。
kind：delete / rewrite / add / waiver。
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class RevisionFeature(Base):
    __tablename__ = "revision_features"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    deliverable_id: Mapped[int] = mapped_column(ForeignKey("deliverables.id"), index=True)
    section_key: Mapped[str] = mapped_column(String(32), default="", index=True)
    source: Mapped[str] = mapped_column(String(16), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    count: Mapped[int] = mapped_column(Integer, default=0)
    change_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    text_hash: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
