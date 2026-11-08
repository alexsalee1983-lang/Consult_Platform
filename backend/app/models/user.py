from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class User(Base):
    """平台用户。过渡期为本地账号 + TOTP 双因素，接入公司 SSO 后保留本表作为用户目录。

    系统角色（顾问、系统管理员、安全审计员等）在 user_roles 表中，见 M1 角色与项目隔离。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    password_hash: Mapped[str] = mapped_column(String(256))          # Argon2id
    totp_secret: Mapped[str] = mapped_column(String(64))             # Base32；S5 加密存储
    totp_last_step: Mapped[int] = mapped_column(BigInteger, default=0)  # 已用过的最近时间步，防止动态码重放
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
