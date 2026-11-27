from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow as _now


class Customization(Base):
    """项目定制项（四期 A30，迁移 0059）：D1 客户要求栏、顾问对话、编辑器三个入口汇成一张表。

    规则只在 app/project/customizations.py：正文过机密级检测才保存；结构类（增删章节）须项目经理确认后生效；
    碰到必备章节、越过红线或资质边界的标 unacceptable 并写平台给的理由。审计不记 text。
    """

    __tablename__ = "customizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    source: Mapped[str] = mapped_column(String(8))                 # d1 / chat / editor
    kind: Mapped[str] = mapped_column(String(16))                  # form 形式类 / wording 口径类 / structure 结构类
    action: Mapped[str] = mapped_column(String(8), default="")     # 结构类：add / remove
    section_title: Mapped[str] = mapped_column(String(128), default="")   # 结构类：增删的章节标题
    deliverable_code: Mapped[str] = mapped_column(String(8), default="")  # 空 = 全部交付件
    section_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 编辑器入口：只对这一章
    one_time: Mapped[bool] = mapped_column(Boolean, default=False)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))   # proposed / pending_pm / active / used / unacceptable / rejected / withdrawn
    reason: Mapped[str] = mapped_column(String(256), default="")   # 不可接受的理由（平台写，不含正文）
    card_id: Mapped[int | None] = mapped_column(ForeignKey("confirmations.id"), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    decided_by: Mapped[str] = mapped_column(String(64), default="")      # 最后一次确认 / 审批 / 撤回的人
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 生效或生效后撤回的时间
