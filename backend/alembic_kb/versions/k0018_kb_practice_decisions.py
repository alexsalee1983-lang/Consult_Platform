"""三期 B17（拆出）：公司做法确认队列每条决定的耗时与修改幅度。

kb_practice_decisions：version_id、reviewer、decision（adopt / modify / reject）、elapsed_ms、change_ratio。不记正文。
down 接 k0017（B35 公司做法类型，#253 未合并；11-30 后按三期计划第七节顺序改接）。

Revision ID: k0018_kb_practice_decisions
Revises: k0017_kb_knowledge_kind
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0018_kb_practice_decisions"
down_revision: str | None = "k0017_kb_knowledge_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kb_practice_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version_id", sa.Integer(), sa.ForeignKey("kb_versions.id"), nullable=False),
        sa.Column("reviewer", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("change_ratio", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_kb_practice_decisions_version_id", "kb_practice_decisions", ["version_id"])


def downgrade() -> None:
    op.drop_index("ix_kb_practice_decisions_version_id", table_name="kb_practice_decisions")
    op.drop_table("kb_practice_decisions")
