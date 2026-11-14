"""M5 ②d：users.password_changed_at（口令有效期，等保自查 8.1.4.1 a）。

已有账号按迁移时间计起，不会一升级就全部到期。

Revision ID: 0029_password_changed_at
Revises: 0028_totp_secret_encrypted
Create Date: 2026-11-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_password_changed_at"
down_revision: str | None = "0028_totp_secret_encrypted"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text("UPDATE users SET password_changed_at = CURRENT_TIMESTAMP"))


def downgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.drop_column("password_changed_at")
