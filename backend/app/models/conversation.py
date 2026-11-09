"""项目区的 Agent 对话。只有发起人能查看和继续（项目成员身份失去后也看不到）。

内容属于项目区数据：进入模型前已过机密级拦截；严格档项目里只有占位符内容。
删除随项目区的保留期管理（M4）。每条对话和每条 Agent 回复都记下当时的提示词版本（md 内容哈希）。
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.db import Base, utcnow


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    agent_code: Mapped[str] = mapped_column(ForeignKey("agents.code"))
    title: Mapped[str] = mapped_column(String(128), default="")
    prompt_version: Mapped[str] = mapped_column(String(16))      # 创建时的提示词版本
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", order_by="Message.id",
                                                     cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))                 # user / assistant
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok / interrupted（流式中断，内容不完整）
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(16), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Agent 回复时附给它的知识库条目（迁移 0015）：[{doc_id, title, heading, layer, citation}]，不含正文
    kb_refs: Mapped[list] = mapped_column(JSON, default=list)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
