"""交付链运行记录（M3 ④ Agent 编排，迁移 0020）。

- AgentRun：一次交付链运行（标准档 3→4→7：纪要 → D3 事实清单 → 差距分析 → D4 现状评估报告初稿）。
  平台自写流程控制（决策记录 2026-11-10，不用 LangGraph）：每一步是后台任务，状态和进度都在这张表里。
  Agent 发了确认卡就停在 waiting，顾问处理完这些卡后自动继续。
- AgentRunStep：每一步的执行记录（哪个 Agent、提示词版本、模型、用量、产出引用）。
  差距分析这一步的 Agent（依据与差距分析）没有写交付件的授权，产出存在 output 里，作为下一步的输入；
  它属于项目区内容，只有项目成员能看，不进审计和日志。
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.db import Base, utcnow


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    chain: Mapped[str] = mapped_column(String(32), default="standard")
    status: Mapped[str] = mapped_column(String(16), default="queued")   # queued/running/waiting/done/failed/cancelled
    step_index: Mapped[int] = mapped_column(Integer, default=0)          # 下一步（或正在跑的一步）在 STEPS 里的位置
    waiting_cards: Mapped[list] = mapped_column(JSON, default=list)      # 等顾问处理的确认卡编号
    card_ids: Mapped[list] = mapped_column(JSON, default=list)           # 本次运行发过的全部确认卡
    error: Mapped[str] = mapped_column(String(200), default="")          # 失败原因（不含客户原文）
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    steps: Mapped[list["AgentRunStep"]] = relationship(
        back_populates="run", order_by="AgentRunStep.id", cascade="all, delete-orphan")


class AgentRunStep(Base):
    __tablename__ = "agent_run_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    step: Mapped[str] = mapped_column(String(16))                # facts / gap / draft
    agent_code: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))              # done / waiting（停下来等补证）/ cancelled（本步执行中被取消）
    output: Mapped[str] = mapped_column(Text, default="")        # 不写交付件的步骤的产出（差距分析）
    refs: Mapped[dict] = mapped_column(JSON, default=dict)       # 产出引用：交付件、章节版本、确认卡编号
    prompt_version: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[AgentRun] = relationship(back_populates="steps")
