"""M5 ②b：users.totp_secret 改存 AES-256-GCM 密文，加长到 255。

已有的明文由后端启动时加密（core/bootstrap.encrypt_plaintext_rows，需配置 TOTP_KEYS），迁移本身不读密钥、不改数据。

Revision ID: 0028_totp_secret_encrypted
Revises: 0027_consent_summary_cards
Create Date: 2026-11-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_totp_secret_encrypted"
down_revision: str | None = "0027_consent_summary_cards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.alter_column("totp_secret", existing_type=sa.String(64), type_=sa.String(255), existing_nullable=False)


def downgrade() -> None:
    # 降级前须先把密文解回明文（降级后旧代码只认明文），否则登录会失败
    with op.batch_alter_table("users") as b:
        b.alter_column("totp_secret", existing_type=sa.String(255), type_=sa.String(64), existing_nullable=False)
