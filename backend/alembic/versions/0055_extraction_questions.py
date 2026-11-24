"""四期 F4③：经验萃取问题表 extraction_questions（不存正文、不连项目）。

Revision ID: 0055_extraction_questions
Revises: 0054_revision_features
Create Date: 2026-11-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055_extraction_questions"
down_revision: str | None = "0054_revision_features"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXED = ("signal", "topic", "status", "assignee_id")


def upgrade() -> None:
    op.create_table(
        "extraction_questions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("signal", sa.String(24), nullable=False),
        sa.Column("topic", sa.String(64), nullable=False),
        sa.Column("projects", sa.Integer(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("filter_reason", sa.String(16), nullable=False),
        sa.Column("skip_reason", sa.String(16), nullable=False),
        sa.Column("assignee_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("upload_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for col in INDEXED:
        op.create_index(f"ix_extraction_questions_{col}", "extraction_questions", [col])


def downgrade() -> None:
    for col in INDEXED:
        op.drop_index(f"ix_extraction_questions_{col}", table_name="extraction_questions")
    op.drop_table("extraction_questions")
