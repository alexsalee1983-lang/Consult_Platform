"""四期 F4②：改稿与驳回特征表 revision_features（不存正文，只存计数、幅度和摘录哈希）。

Revision ID: 0054_revision_features
Revises: 0052_agent_md_versions（main 链头；0053 预留给未合并的 PR）
Create Date: 2026-11-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054_revision_features"
down_revision: str | None = "0045_nine_agents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "revision_features",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("deliverable_id", sa.Integer(), sa.ForeignKey("deliverables.id"), nullable=False),
        sa.Column("section_key", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("change_ratio", sa.Float(), nullable=False),
        sa.Column("text_hash", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for col in ("project_id", "deliverable_id", "section_key", "source"):
        op.create_index(f"ix_revision_features_{col}", "revision_features", [col])


def downgrade() -> None:
    for col in ("project_id", "deliverable_id", "section_key", "source"):
        op.drop_index(f"ix_revision_features_{col}", table_name="revision_features")
    op.drop_table("revision_features")
