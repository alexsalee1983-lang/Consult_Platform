"""四期 B44④：专家档案（擅长产品、行业）与按专长派卡。

kb_expert_profiles 每位知识专家一行；kb_review_assignments 待审版本派给谁（没有记录 = 发全体知识专家）。
不含正文。

Revision ID: k0023_kb_expert_assign
Revises: k0022_kb_gap_manual
Create Date: 2026-11-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0023_kb_expert_assign"
down_revision: str | None = "k0022_kb_gap_manual"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kb_expert_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("products", sa.JSON(), nullable=False),
        sa.Column("industries", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "kb_review_assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version_id", sa.Integer(), sa.ForeignKey("kb_versions.id", ondelete="CASCADE"), nullable=False,
                  unique=True),
        sa.Column("assignee", sa.String(64), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_kb_review_assignments_assignee", "kb_review_assignments", ["assignee"])


def downgrade() -> None:
    op.drop_index("ix_kb_review_assignments_assignee", table_name="kb_review_assignments")
    op.drop_table("kb_review_assignments")
    op.drop_table("kb_expert_profiles")
