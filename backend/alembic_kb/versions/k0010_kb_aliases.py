"""三期 B15：知识条目别名表 kb_item_aliases（不回填）。

旧编号（legacy，B15 换号时登记）、跨环境迁移时对方的编号（transfer）、合并前的编号（merge）。
app/kb/refs.resolve 在 kb_items.doc_id 查不到时按别名查；别名永不删除，与 kb_items.doc_id 互斥（代码层检查）。
本迁移只建空表，downgrade 直接删表。

Revision ID: k0010_kb_aliases
Revises: k0009_kb_upload_project
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0010_kb_aliases"
down_revision: str | None = "k0009_kb_upload_project"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kb_item_aliases",
        sa.Column("alias", sa.String(40), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["kb_items.id"], name="fk_kb_item_aliases_item_id"),
        sa.PrimaryKeyConstraint("alias", name="pk_kb_item_aliases"),
    )
    op.create_index("ix_kb_item_aliases_item_id", "kb_item_aliases", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_kb_item_aliases_item_id", table_name="kb_item_aliases")
    op.drop_table("kb_item_aliases")
