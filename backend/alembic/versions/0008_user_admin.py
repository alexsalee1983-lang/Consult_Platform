"""M1：用户管理界面。users 增加 must_change_password：管理员在网页上建账号或重置口令后，用户首次登录须先改口令。

Revision ID: 0008_user_admin
Revises: 0007_conversations
Create Date: 2026-11-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_user_admin"
down_revision: str | None = "0007_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 已有账号（命令行建的、测试账号）口令由本人设置，不要求改
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False,
                                     server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("must_change_password")
