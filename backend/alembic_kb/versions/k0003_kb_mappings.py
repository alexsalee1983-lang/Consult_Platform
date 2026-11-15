"""二期 B3：控制映射矩阵 kb_mappings（映射矩阵条目各版本正文表格解析出的映射行，app/kb/mapping.py）。

Revision ID: k0003_kb_mappings
Revises: k0002_kb_tags
Create Date: 2026-11-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0003_kb_mappings"
down_revision: str | None = "k0002_kb_tags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kb_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version_id", sa.Integer(), sa.ForeignKey("kb_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("source_std", sa.String(100), nullable=False),
        sa.Column("source_ref", sa.String(40), nullable=False),
        sa.Column("target_std", sa.String(100), nullable=False),
        sa.Column("target_ref", sa.String(40), nullable=False),
        sa.Column("relation", sa.String(16), nullable=False),
        sa.Column("note", sa.String(500), nullable=False),
        sa.UniqueConstraint("version_id", "seq", name="uq_kb_mappings_version_seq"),
    )
    for col in ("version_id", "source_std", "source_ref", "target_std", "target_ref"):
        op.create_index(f"ix_kb_mappings_{col}", "kb_mappings", [col])


def downgrade() -> None:
    op.drop_table("kb_mappings")
