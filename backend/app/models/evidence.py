"""项目区资料与证据编号（M3，迁移 0017）。

- ProjectDocument：客户提供的资料（Word / PDF / Markdown / 文本），原件和解析结果存对象存储项目区，
  键里不用原文件名；文件名只在本表和接口里出现，不进审计和日志。
- Evidence：项目内的证据编号。文档 D-01、访谈纪要 I-01、现场观察 O-01，按项目、按类别各自从 01 递增，
  编号一经分配不再变化、不复用（交付件和 Agent 输出靠它追溯，公共规范 4.1）。
  访谈纪要只写受访者角色，不写姓名（公共规范第 5 节“个人信息”）。
"""
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.db import Base, utcnow

EVIDENCE_PREFIX = {"document": "D", "interview": "I", "observation": "O"}
EVIDENCE_KIND_NAMES = {"document": "客户资料", "interview": "访谈纪要", "observation": "现场观察"}


class ProjectDocument(Base):
    __tablename__ = "project_documents"
    __table_args__ = (UniqueConstraint("project_id", "sha256", name="uq_project_documents_sha"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    object_key: Mapped[str] = mapped_column(String(512), unique=True)   # 对象存储项目区的键
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="")             # docx / pdf / markdown / text
    status: Mapped[str] = mapped_column(String(16), default="received")   # received 待解析 / parsed / failed
    error: Mapped[str] = mapped_column(String(200), default="")
    parsed_key: Mapped[str] = mapped_column(String(512), default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    section_count: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Evidence(Base):
    __tablename__ = "evidences"
    __table_args__ = (UniqueConstraint("project_id", "code", name="uq_evidences_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    code: Mapped[str] = mapped_column(String(16))        # D-01 / I-01 / O-01
    kind: Mapped[str] = mapped_column(String(16))        # document / interview / observation
    seq: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    interviewee_role: Mapped[str] = mapped_column(String(100), default="")   # 访谈纪要：受访者角色，不写姓名
    occurred_on: Mapped[date | None] = mapped_column(Date, nullable=True)    # 访谈 / 观察日期
    content: Mapped[str] = mapped_column(Text, default="")                   # 纪要、观察正文；文档为空（正文在对象存储）
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("project_documents.id"), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    document: Mapped[ProjectDocument | None] = relationship()
