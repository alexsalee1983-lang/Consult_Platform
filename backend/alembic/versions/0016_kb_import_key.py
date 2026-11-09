"""M2：知识条目的导入标识 kb_items.import_key（批量导入、评测题对应条目用）。

Revision ID: 0016_kb_import_key
Revises: 0015_message_references
Create Date: 2026-11-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_kb_import_key"
down_revision: str | None = "0015_message_references"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_items") as batch:
        batch.add_column(sa.Column("import_key", sa.String(length=64), nullable=True))
        batch.create_unique_constraint("uq_kb_items_import_key", ["import_key"])


def downgrade() -> None:
    with op.batch_alter_table("kb_items") as batch:
        batch.drop_constraint("uq_kb_items_import_key", type_="unique")
        batch.drop_column("import_key")
