"""M3 ⑧：项目经理驳回质量评审 Agent 提出的阻断问题（review_waivers，只追加）。

负责人 2026-11-12 决定：评审 Agent 判出的阻断，项目经理可逐条驳回，必须写理由，进审计（不记理由原文），
在签署记录里显示；平台硬规则判出的阻断（缺正文、机密级、测评 / 认证结论用语、来源核对不上）不能驳回。

Revision ID: 0022_review_waivers
Revises: 0021_quality_signoff
Create Date: 2026-11-12
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_review_waivers"
down_revision: str | None = "0021_quality_signoff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_waivers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("review_id", sa.Integer(), sa.ForeignKey("quality_reviews.id"), nullable=False),
        sa.Column("issue_index", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("review_id", "issue_index", name="uq_review_waivers_issue"),
    )
    op.create_index("ix_review_waivers_project_id", "review_waivers", ["project_id"])
    op.create_index("ix_review_waivers_review_id", "review_waivers", ["review_id"])


def downgrade() -> None:
    op.drop_index("ix_review_waivers_review_id", table_name="review_waivers")
    op.drop_index("ix_review_waivers_project_id", table_name="review_waivers")
    op.drop_table("review_waivers")
