"""M3 ⑤：质量门禁与签署（quality_reviews、sign_offs）。

质量评审与异议应答 Agent 的评审结果（阻断 / 重要 / 建议）和签署流程（提交签署、签署、退回）各一张表。
质量评审 Agent 读交付件正文由平台按发起人的成员身份交给它，不新增工具、不改 Agent × 工具授权。

Revision ID: 0021_quality_signoff
Revises: 0020_agent_runs
Create Date: 2026-11-12
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_quality_signoff"
down_revision: str | None = "0020_agent_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quality_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("deliverable_id", sa.Integer(), sa.ForeignKey("deliverables.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("versions", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("conclusion", sa.String(length=16), nullable=False),
        sa.Column("blocking", sa.Integer(), nullable=False),
        sa.Column("major", sa.Integer(), nullable=False),
        sa.Column("minor", sa.Integer(), nullable=False),
        sa.Column("issues", sa.JSON(), nullable=False),
        sa.Column("report", sa.Text(), nullable=False),
        sa.Column("error", sa.String(length=200), nullable=False),
        sa.Column("agent_code", sa.String(length=32), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_quality_reviews_project_id", "quality_reviews", ["project_id"])
    op.create_index("ix_quality_reviews_deliverable_id", "quality_reviews", ["deliverable_id"])
    op.create_table(
        "sign_offs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("deliverable_id", sa.Integer(), sa.ForeignKey("deliverables.id"), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("versions", sa.JSON(), nullable=False),
        sa.Column("review_id", sa.Integer(), sa.ForeignKey("quality_reviews.id"), nullable=True),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sign_offs_project_id", "sign_offs", ["project_id"])
    op.create_index("ix_sign_offs_deliverable_id", "sign_offs", ["deliverable_id"])


def downgrade() -> None:
    op.drop_index("ix_sign_offs_deliverable_id", table_name="sign_offs")
    op.drop_index("ix_sign_offs_project_id", table_name="sign_offs")
    op.drop_table("sign_offs")
    op.drop_index("ix_quality_reviews_deliverable_id", table_name="quality_reviews")
    op.drop_index("ix_quality_reviews_project_id", table_name="quality_reviews")
    op.drop_table("quality_reviews")
