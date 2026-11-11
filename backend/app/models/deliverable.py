"""交付件分章节与版本（M3，迁移 0018）。

- DeliverableSection：交付件的一章（目录里的一行），按 seq 排序；章节标题可改，编号就是排序号。
- SectionVersion：一章的一个版本。只追加、不修改：每次 Agent 或顾问写入都新建一个版本，旧版本保留，
  版本对比和追溯都靠它。正文是 Markdown，来源用方括号标注（公共规范 4.1）：
  客户证据 [I-02] [I-02-05] [D-12] [O-05]，知识库条目 [L4-OPS-GEN-REF-001]，模型推断 [推断]。
  写入时解析这些标注并核对（证据在本项目里存在、知识库条目存在），结果存在 sources 里，界面据此显示来源构成。
- 交付件的 revision 每写入一次章节就加 1（界面上的 v3）。
正文属于项目区内容，不进审计和日志；审计只记交付件编号、章节号、版本号、字数和摘要哈希。
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.db import Base, utcnow


class DeliverableSection(Base):
    __tablename__ = "deliverable_sections"
    __table_args__ = (UniqueConstraint("deliverable_id", "seq", name="uq_deliverable_sections_seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    deliverable_id: Mapped[int] = mapped_column(ForeignKey("deliverables.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)                    # 章节号（1 起），目录顺序
    title: Mapped[str] = mapped_column(String(200))
    current_version: Mapped[int] = mapped_column(Integer, default=0)   # 0 = 还没有正文
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    versions: Mapped[list["SectionVersion"]] = relationship(
        back_populates="section", order_by="SectionVersion.version", cascade="all, delete-orphan")


class SectionVersion(Base):
    __tablename__ = "section_versions"
    __table_args__ = (UniqueConstraint("section_id", "version", name="uq_section_versions_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    section_id: Mapped[int] = mapped_column(ForeignKey("deliverable_sections.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    body_sha: Mapped[str] = mapped_column(String(64))
    sources: Mapped[dict] = mapped_column(JSON, default=dict)     # 解析出的来源标注和核对结果，见 app/project/deliverables.py
    note: Mapped[str] = mapped_column(String(200), default="")    # 修改说明
    author: Mapped[str] = mapped_column(String(64))               # 发起的用户（Agent 写入时是发起对话的顾问）
    agent_code: Mapped[str] = mapped_column(String(64), default="")   # Agent 写入时的 Agent 代号；顾问手改为空
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    section: Mapped[DeliverableSection] = relationship(back_populates="versions")
