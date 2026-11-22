"""三期 B29 最小版：知识使用反馈 knowledge_feedback（主库、项目区数据，不用知识区专用的 kb_ 前缀）（有用 / 过时 / 不对，附言只存库、不进日志）。

down_revision 暂接 0038：0039～0046（F1、G2②、A13、A24、A15、F2 等）未合并，合并顺序定了再改链。

Revision ID: 0047_kb_feedback
Revises: 0038_judgment_events
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_kb_feedback"
down_revision: str | None = "0038_judgment_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("doc_id", sa.String(32), nullable=False),
        sa.Column("surface", sa.String(8), nullable=False),
        sa.Column("ref_id", sa.Integer(), nullable=False),
        sa.Column("rating", sa.String(8), nullable=False),
        sa.Column("note", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "surface", "ref_id", "doc_id", name="uq_knowledge_feedback_ref"),
    )
    op.create_index("ix_knowledge_feedback_project_id", "knowledge_feedback", ["project_id"])
    op.create_index("ix_knowledge_feedback_doc_id", "knowledge_feedback", ["doc_id"])
    op.create_index("ix_knowledge_feedback_updated_at", "knowledge_feedback", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_feedback_updated_at", table_name="knowledge_feedback")
    op.drop_index("ix_knowledge_feedback_doc_id", table_name="knowledge_feedback")
    op.drop_index("ix_knowledge_feedback_project_id", table_name="knowledge_feedback")
    op.drop_table("knowledge_feedback")
