from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import true as sql_true

from ..core.db import Base, utcnow as _now


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # 客户代号，如 BANK-A
    service: Mapped[str] = mapped_column(String(64))          # 服务产品
    industry: Mapped[str] = mapped_column(String(32))
    tier: Mapped[str] = mapped_column(String(16), default="standard")  # standard 标准档 / strict 严格档
    phase: Mapped[int] = mapped_column(Integer, default=0)   # 0 启动 … 5 验收
    week: Mapped[int] = mapped_column(Integer, default=1)
    total_weeks: Mapped[int] = mapped_column(Integer, default=12)
    posture_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_milestone: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # 保留与删除（M4 ①，迁移 0023，规则见 app/project/retention.py）
    accepted_on: Mapped[date | None] = mapped_column(Date, nullable=True)          # 验收日，未验收为空
    retention_months: Mapped[int] = mapped_column(Integer, server_default="6")  # 只用库默认值（M0 迁移测试按旧表结构插入）
    deletion_due_on: Mapped[date | None] = mapped_column(Date, nullable=True)      # 到期日 = 验收日 + 保留月数
    # 删除（M4 ②，迁移 0025，规则见 app/project/deletion.py）
    flowback_allowed: Mapped[bool] = mapped_column(Boolean, server_default=sql_true())   # 客户不同意回流时为 False
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    deliverables: Mapped[list["Deliverable"]] = relationship(
        back_populates="project", order_by="Deliverable.code", cascade="all, delete-orphan"
    )
    confirmations: Mapped[list["Confirmation"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Deliverable(Base):
    __tablename__ = "deliverables"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    code: Mapped[str] = mapped_column(String(8))      # D1 … D8
    title: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="not_started")
    # not_started 未开始 / drafting 起草中 / review 待审核 / signed 已签署
    revision: Mapped[int] = mapped_column(Integer, default=0)   # 每写入一次章节加 1（界面上的 v3），迁移 0018
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[str] = mapped_column(String(64), default="")

    project: Mapped[Project] = relationship(back_populates="deliverables")
    sections: Mapped[list["DeliverableSection"]] = relationship(  # noqa: F821
        order_by="DeliverableSection.seq", cascade="all, delete-orphan")


class Confirmation(Base):
    """需要顾问做判断的事项（确认卡）：方案取舍、来源冲突、证据不足、范围确认、入库确认。

    Agent 经工具 confirm.create 发卡（迁移 0019 起），附选项、推荐和理由、影响；顾问选一个选项或写自己的意见，
    处理后 Agent 再继续（S3 第 ④ 个 PR 的编排据此暂停和继续）。规则见 app/project/confirmations.py。
    """

    __tablename__ = "confirmations"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(256))
    agent: Mapped[str] = mapped_column(String(64))                   # 显示名，如“方案设计 Agent”
    status: Mapped[str] = mapped_column(String(16), default="open")  # open / resolved
    resolution: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # ---- 迁移 0019 ----
    agent_code: Mapped[str] = mapped_column(String(32), default="")  # 发卡的 Agent 代号；演示数据为空
    options: Mapped[list] = mapped_column(JSON, default=list)        # [{"key": "A", "text": "……"}]
    recommendation: Mapped[str] = mapped_column(String(8), default="")   # 推荐的选项 key
    reason: Mapped[str] = mapped_column(Text, default="")            # 推荐理由（引用前提、差距编号、依据）
    impact: Mapped[str] = mapped_column(Text, default="")            # 选不同选项分别影响什么
    deliverable_code: Mapped[str] = mapped_column(String(8), default="")   # 关联的交付件（可空），如 D5
    section_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 关联的章（可空）
    created_by: Mapped[str] = mapped_column(String(64), default="")  # 发起对话的顾问
    choice: Mapped[str] = mapped_column(String(8), default="")       # 顾问选的选项 key；自己写意见时为空
    resolved_by: Mapped[str] = mapped_column(String(64), default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="confirmations")


class ProjectDeletion(Base):
    """项目资料删除申请与删除记录（M4 ②）。完成后就是可交给客户的删除记录：只记编号、日期、计数、确认人和回流情况。

    kind：expiry 到期（平台规则发起，requested_by="platform"）/ early 客户书面要求提前删除（项目经理发起）。
    status：pending 待项目负责人确认 / confirmed 已确认、删除执行中 / done 已删除 / cancelled 已撤销。
    flowback：done 已回流（flowback_refs 为投料编号）/ none 无可回流内容 / client_refused 客户不同意回流。
    """

    __tablename__ = "project_deletions"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), index=True)
    requested_by: Mapped[str] = mapped_column(String(64))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    request_ref: Mapped[str] = mapped_column(String(64), default="")          # 客户书面要求的文号（不写客户名称）
    received_on: Mapped[date | None] = mapped_column(Date, nullable=True)     # 收到书面要求的日期
    due_by: Mapped[date] = mapped_column(Date)                                # 应完成日期
    confirmed_by: Mapped[str] = mapped_column(String(64), default="")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    flowback: Mapped[str] = mapped_column(String(16), default="")
    flowback_refs: Mapped[list] = mapped_column(JSON, default=list)
    cancelled_by: Mapped[str] = mapped_column(String(64), default="")
    cancel_reason: Mapped[str] = mapped_column(String(200), default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    counts: Mapped[dict] = mapped_column(JSON, default=dict)                  # 删除了多少：文件、证据、交付件版本……
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)

    project: Mapped[Project] = relationship()


class ProjectConfig(Base):
    """立项快照（二期 A3，迁移 0032，docs/接口约定.md 第四节）：产品 + 所选模块合并后的完整配置，立项后不变。

    只有配置（产品、模块、任务说明、门禁），没有客户资料；项目删除时保留（项目行也保留）。
    """
    __tablename__ = "project_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), unique=True)
    product: Mapped[str] = mapped_column(String(8))
    modules: Mapped[list] = mapped_column(JSON, default=list)            # 模块代号
    product_version: Mapped[str] = mapped_column(String(16))
    module_versions: Mapped[dict] = mapped_column(JSON, default=dict)    # 代号 → 版本
    snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_by: Mapped[str] = mapped_column(String(64), default="")
