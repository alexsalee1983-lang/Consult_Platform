"""四期 F4③：经验萃取问题（迁移 0055）。规则只在 app/kb/extraction.py。

一行一道问题。问题只按“信号 + 主题”聚合，不连项目：主题是知识槽位键或章节键（交付件代号.章节序号，如 D4.1），
不存正文、不存项目代号，只存涉及项目数和计数，所以不随单个项目删除。
signal：knowledge_gap 知识不足卡 / revision_cluster 改稿聚类 / review_waiver 评审驳回。
status：filtered 被过滤（filter_reason：known 知识库已有 / recent 近期问过 / low 低置信）/ pending 待派 /
asked 已派 / answered 已回答 / skipped 跳过（skip_reason：expert 专家跳过 / timeout 超时）。
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class ExtractionQuestion(Base):
    __tablename__ = "extraction_questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal: Mapped[str] = mapped_column(String(24), index=True)
    topic: Mapped[str] = mapped_column(String(64), index=True)
    projects: Mapped[int] = mapped_column(Integer, default=0)
    count: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    filter_reason: Mapped[str] = mapped_column(String(16), default="")
    skip_reason: Mapped[str] = mapped_column(String(16), default="")
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    asked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    upload_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 回答投料后的投料编号（知识区，不建外键）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
