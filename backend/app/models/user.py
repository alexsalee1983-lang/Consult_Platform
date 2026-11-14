from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, false
from sqlalchemy.orm import Mapped, mapped_column

from ..core import secretbox
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
    # TOTP 共享密钥：库里存 AES-256-GCM 密文（core/secretbox.py，等保自查 ②b），代码只经 totp_secret 属性读写明文
    totp_secret_enc: Mapped[str] = mapped_column("totp_secret", String(255))
    totp_last_step: Mapped[int] = mapped_column(BigInteger, default=0)  # 已用过的最近时间步，防止动态码重放
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 管理员在网页上建账号或重置口令后为 True：登录后只能改口令，改完才能用其他功能
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 口令最近一次设置的时间（口令有效期 ②d）；改口令一律经 set_password_hash
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def totp_secret(self) -> str:
        return secretbox.decrypt(self.totp_secret_enc)

    @totp_secret.setter
    def totp_secret(self, value: str) -> None:
        self.totp_secret_enc = secretbox.encrypt(value)

    def set_password_hash(self, value: str) -> None:
        self.password_hash = value
        self.password_changed_at = utcnow()
