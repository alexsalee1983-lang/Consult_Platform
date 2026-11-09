"""M2：投料解析结果字段（kb_uploads 增加文件类型、解析结果位置、页数、章节数、片段数、字数、解析时间）。

Revision ID: 0012_kb_upload_parse
Revises: 0011_kb
Create Date: 2026-11-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_kb_upload_parse"
down_revision: str | None = "0011_kb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INT_COLUMNS = ("page_count", "section_count", "chunk_count", "char_count")


def upgrade() -> None:
    with op.batch_alter_table("kb_uploads") as batch:
        batch.add_column(sa.Column("kind", sa.String(length=16), nullable=False, server_default=""))
        batch.add_column(sa.Column("parsed_key", sa.String(length=512), nullable=False, server_default=""))
        for name in _INT_COLUMNS:
            batch.add_column(sa.Column(name, sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("kb_uploads") as batch:
        for name in ("parsed_at", *_INT_COLUMNS, "parsed_key", "kind"):
            batch.drop_column(name)
