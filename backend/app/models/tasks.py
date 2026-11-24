"""三期 B17②“我的任务”的退回事件（迁移 0043）：顾问或专家把 Agent 产出的待办退回，排队由 Agent 重写后重新排序。

退回意见先过机密级检测，只存库，不进审计和应用日志；任务参数只放退回编号。规则只在 app/project/my_tasks.py。
status：queued 已排队 / waiting_agent 已交给重写入口、等 Agent / done 已重写（重新进入我的任务）。
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class TaskReturn(Base):
    __tablename__ = "task_returns"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))                       # 待办类别，见 my_tasks.KINDS
    ref_id: Mapped[int] = mapped_column(Integer, index=True)            # 对应对象的编号（确认卡、知识版本等）
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(String(200), default="")       # 退回意见（只存库）
    status: Mapped[str] = mapped_column(String(16), default="queued")
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
