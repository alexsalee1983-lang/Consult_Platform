"""立项与合同要点卡（四期 A32，迁移 0061；docs/交付链输入输出方案.md 4.1、4.6、4.7）。

- ContractCard：合同要点卡，项目基础数据，全程保留（合同原文照旧抽取后删除）。每个要点存
  {value, state, source}：state 为 verified 已核对 / unstated 合同未写明 / clarify 待与客户澄清 / extra 合同外新增。
  version 从 1 递增，只有补充协议或合同到达才出新版本，旧版保留；provisional = 先行启动的临时卡（依据不是合同）。
- ProjectAuthorization：授权台账，只记签署状态（附件编号、版本、日期、记录人），不存附件正文；每次变更追加一行，
  同一 kind 最新一行即当前状态。
规则只在 app/project/intake.py；审计只记事件，不记要点原文。
"""
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class ContractCard(Base):
    __tablename__ = "contract_cards"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_contract_card_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    provisional: Mapped[bool] = mapped_column(Boolean, default=False)
    basis_kind: Mapped[str] = mapped_column(String(16))            # contract / award / letter / proposal / presales / email / manual
    status: Mapped[str] = mapped_column(String(16), default="draft")   # extracting / draft / checked / failed
    fields: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(String(200), default="")
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    checked_by: Mapped[str] = mapped_column(String(64), default="")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectAuthorization(Base):
    __tablename__ = "project_authorizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))                  # 见 app/project/intake.AUTH_KINDS
    status: Mapped[str] = mapped_column(String(16))                # signed 已签 / withdrawn 已撤回
    version: Mapped[str] = mapped_column(String(32), default="")   # 附件版本号
    signed_on: Mapped[date] = mapped_column(Date)
    recorded_by: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
