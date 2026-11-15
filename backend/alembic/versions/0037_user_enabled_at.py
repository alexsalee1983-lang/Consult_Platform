"""二期 C5 O7：users.enabled_at（系统管理员最近一次启用账号的时间，长期未登录自动停用从这里重新计时）。

Revision ID: 0037_user_enabled_at
Revises: 0032_project_configs（0033、0035、0036 合并后改接链头）
Create Date: 2026-11-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_user_enabled_at"
down_revision: str | None = "0036_kb_reviewer_agent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.drop_column("enabled_at")
