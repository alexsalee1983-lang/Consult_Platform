from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class LlmUsage(Base):
    """大模型用量：每次经网关的调用一条，按人、项目、模型统计。只记计量数据，不记提问和回复内容。"""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    project_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    level: Mapped[str] = mapped_column(String(16))            # reasoning / standard / light
    model: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16))           # ok / error / blocked
    demo: Mapped[bool] = mapped_column(Boolean, default=False)
    stream: Mapped[bool] = mapped_column(Boolean, default=False)
    pseudonymized: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=1)  # 含重试的请求次数
