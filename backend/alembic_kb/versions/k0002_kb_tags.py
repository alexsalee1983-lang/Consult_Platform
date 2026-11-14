"""二期 B2：知识条目加检索加权标签 tags（JSON 列表，默认空）。

项目勾选的模块在配置里声明 kb_tags，带同名标签的条目在 kb.search 里加权（app/kb/search.py）。
已有条目一律为空列表，不影响现有排序。

Revision ID: k0002_kb_tags
Revises: k0001_kb_baseline
Create Date: 2026-11-14
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0002_kb_tags"
down_revision: str | None = "k0001_kb_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_items") as batch:
        batch.add_column(sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))


def downgrade() -> None:
    with op.batch_alter_table("kb_items") as batch:
        batch.drop_column("tags")
