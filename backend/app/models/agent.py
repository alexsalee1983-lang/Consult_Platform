"""Agent、工具与 Agent × 工具授权（架构设计第五节“Agent × 工具授权”）。

授权清单由迁移写入，改动须经代码审阅；没有修改授权的接口。
另有硬规则写在代码里（app/agents/tools.py）：知识侧 Agent 不能用项目区工具，交付侧 Agent 不能写知识区，
即使有人直接改了授权表也不生效。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class Agent(Base):
    __tablename__ = "agents"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)     # 与 agents/roles/*.md 的 name 一致
    name: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(16))                       # delivery 交付侧 / knowledge 知识侧
    phase: Mapped[str] = mapped_column(String(16))                      # 一期 / 预留
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Tool(Base):
    __tablename__ = "tools"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)     # 如 project.read、kb.search
    name: Mapped[str] = mapped_column(String(32))
    zone: Mapped[str] = mapped_column(String(16))                       # knowledge 知识区 / project 项目区 / staging 待入库区 / none
    access: Mapped[str] = mapped_column(String(8))                      # read / write


class AgentToolGrant(Base):
    __tablename__ = "agent_tool_grants"

    agent_code: Mapped[str] = mapped_column(ForeignKey("agents.code"), primary_key=True)
    tool_code: Mapped[str] = mapped_column(ForeignKey("tools.code"), primary_key=True)
    granted_by: Mapped[str] = mapped_column(String(64))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentMdVersion(Base):
    """四期 F3②e：Agent md 版本库（迁移 0052）。平台取用 md 时按内容哈希存一份，部署机只部署 main。"""
    __tablename__ = "agent_md_versions"
    __table_args__ = (UniqueConstraint("agent_code", "version", name="uq_agent_md_versions_code_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_code: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[str] = mapped_column(String(12))                    # 公共规范 + 角色 md 的内容哈希
    md_version: Mapped[str] = mapped_column(String(32), default="")    # md 自己写的 version
    common_text: Mapped[str] = mapped_column(Text)
    role_text: Mapped[str] = mapped_column(Text)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentMdPin(Base):
    """四期 F3②e：回滚指针。有指针时运行时按指针取 md，撤销即删除指针。"""
    __tablename__ = "agent_md_pins"

    agent_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[str] = mapped_column(String(12))
    incident_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pinned_by: Mapped[str] = mapped_column(String(64))
    pinned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
