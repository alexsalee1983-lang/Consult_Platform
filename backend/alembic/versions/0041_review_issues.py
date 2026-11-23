"""三期 A13：评审问题台账 review_issues（问题跨轮次固定编号 Q-001、状态 open / resolved / waived）。

down_revision 暂接 0038：0039（F1 #279）、0040（G2② #249）未合并，合并顺序定了再改链。

Revision ID: 0041_review_issues
Revises: 0038_judgment_events
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_review_issues"
down_revision: str | None = "0038_judgment_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_issues",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("deliverable_id", sa.Integer(), sa.ForeignKey("deliverables.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(8), nullable=False),
        sa.Column("fingerprint", sa.String(16), nullable=False),
        sa.Column("by", sa.String(8), nullable=False),
        sa.Column("category", sa.String(16), nullable=False, server_default=""),
        sa.Column("status", sa.String(12), nullable=False, server_default="open"),
        sa.Column("first_review_id", sa.Integer(), sa.ForeignKey("quality_reviews.id"), nullable=False),
        sa.Column("last_review_id", sa.Integer(), sa.ForeignKey("quality_reviews.id"), nullable=False),
        sa.Column("resolved_review_id", sa.Integer(), sa.ForeignKey("quality_reviews.id"), nullable=True),
        sa.Column("waiver_id", sa.Integer(), sa.ForeignKey("review_waivers.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("deliverable_id", "seq", name="uq_review_issues_seq"),
    )
    op.create_index("ix_review_issues_project_id", "review_issues", ["project_id"])
    op.create_index("ix_review_issues_deliverable_id", "review_issues", ["deliverable_id"])
    op.create_index("ix_review_issues_fingerprint", "review_issues", ["fingerprint"])


def downgrade() -> None:
    op.drop_index("ix_review_issues_fingerprint", table_name="review_issues")
    op.drop_index("ix_review_issues_deliverable_id", table_name="review_issues")
    op.drop_index("ix_review_issues_project_id", table_name="review_issues")
    op.drop_table("review_issues")
