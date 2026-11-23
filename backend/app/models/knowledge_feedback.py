"""知识使用反馈（三期 B29 最小版，迁移 0047）：顾问在交付件审阅、顾问对话里对引用的知识条目标“有用 / 过时 / 不对”。

属于项目区数据（落主库，按项目删除）；一个顾问对同一处引用（某章节版本或某条 Agent 回复里的某条知识）只留一条，
再点改成新的选择。附言最多 200 字、先过机密级检测，严格档项目不收附言；审计和日志只记长度和摘要哈希，不记原文。
规则只在 app/kb/feedback.py。
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class KnowledgeFeedback(Base):
    __tablename__ = "knowledge_feedback"
    __table_args__ = (UniqueConstraint("user_id", "surface", "ref_id", "doc_id", name="uq_knowledge_feedback_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    doc_id: Mapped[str] = mapped_column(String(32), index=True)
    surface: Mapped[str] = mapped_column(String(8))            # review 交付件审阅（ref_id = 章节版本）/ chat 顾问对话（ref_id = 消息）
    ref_id: Mapped[int] = mapped_column(Integer)
    rating: Mapped[str] = mapped_column(String(8))             # useful / outdated / wrong
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
