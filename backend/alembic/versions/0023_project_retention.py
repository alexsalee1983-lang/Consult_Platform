"""M4 ①：项目验收与保留期（projects.accepted_on / retention_months / deletion_due_on）。

客户承诺（docs/客户数据告知模板与合同条款.md 第六节、第 X.3 条）：验收后项目资料保留 6 个月（或双方另行约定的期限），
到期经人工确认后删除并形成删除记录。本迁移只记验收日、保留月数和到期日；删除在 M4 ② 实现。

Revision ID: 0023_project_retention
Revises: 0022_review_waivers
Create Date: 2026-11-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_project_retention"
down_revision: str | None = "0022_review_waivers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("accepted_on", sa.Date(), nullable=True))
        batch.add_column(sa.Column("retention_months", sa.Integer(), nullable=False, server_default="6"))
        batch.add_column(sa.Column("deletion_due_on", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("deletion_due_on")
        batch.drop_column("retention_months")
        batch.drop_column("accepted_on")
