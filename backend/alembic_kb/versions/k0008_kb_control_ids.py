"""4A 埋点：知识条目挂公司控制项 control_ids（JSON 列表，默认空；4A 架构设计 v4.0 §7.2）。

条目级字段，同 tags：不进版本内容、不走审核；控制项中枢 v1（五期）之前只记不用。

Revision ID: k0008_kb_control_ids
Revises: k0007_kb_recheck
Create Date: 2026-11-18
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0008_kb_control_ids"
down_revision: str | None = "k0007_kb_recheck"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_items") as batch:
        batch.add_column(sa.Column("control_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))


def downgrade() -> None:
    with op.batch_alter_table("kb_items") as batch:
        batch.drop_column("control_ids")
