"""M2 规则调整：知识版本“单人发布”标记（kb_versions.single_review）；审核唯一约束加上审核角色。

决策记录 2026-11-13：知识发布改为两人原则；公开资料在 KB_SINGLE_REVIEWER_PUBLIC=true 时可由作者本人审核发布，
这样发布的版本标记 single_review，等其他知识专家补复审（复审确认）后清除。
同一人持两个角色时可以完成两项审核，所以 kb_reviews 的唯一约束从（版本, 审核人）改为（版本, 审核人, 审核角色）。

Revision ID: 0024_kb_single_review
Revises: 0023_project_retention
Create Date: 2026-11-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_kb_single_review"
down_revision: str | None = "0023_project_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_versions") as batch:
        batch.add_column(sa.Column("single_review", sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table("kb_reviews") as batch:
        batch.drop_constraint("uq_kb_reviews_version_reviewer", type_="unique")
        batch.create_unique_constraint("uq_kb_reviews_version_reviewer_role", ["version_id", "reviewer", "review_role"])


def downgrade() -> None:
    # 回退前须没有“同一人审同一版本两项”的记录，否则旧约束建不起来
    with op.batch_alter_table("kb_reviews") as batch:
        batch.drop_constraint("uq_kb_reviews_version_reviewer_role", type_="unique")
        batch.create_unique_constraint("uq_kb_reviews_version_reviewer", ["version_id", "reviewer"])
    with op.batch_alter_table("kb_versions") as batch:
        batch.drop_column("single_review")
