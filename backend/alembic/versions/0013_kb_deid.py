"""M2：去标识审查。kb_deid_reports 增加 Agent 复查结果与人工确认字段；kb_uploads 增加原件删除时间。

Revision ID: 0013_kb_deid
Revises: 0012_kb_upload_parse
Create Date: 2026-11-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_kb_deid"
down_revision: str | None = "0012_kb_upload_parse"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STR = (("created_by", 64), ("agent_status", 16), ("agent_note", 200), ("conclusion", 16), ("risk_level", 8),
        ("deid_key", 512), ("safe_filename", 255), ("comment", 1000))


def upgrade() -> None:
    with op.batch_alter_table("kb_deid_reports") as batch:
        for name, length in _STR:
            batch.add_column(sa.Column(name, sa.String(length=length), nullable=False, server_default=""))
        batch.add_column(sa.Column("total", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("combination_risks", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("manual_items", sa.JSON(), nullable=False, server_default="[]"))
    with op.batch_alter_table("kb_uploads") as batch:
        batch.add_column(sa.Column("raw_deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("kb_uploads") as batch:
        batch.drop_column("raw_deleted_at")
    with op.batch_alter_table("kb_deid_reports") as batch:
        for name in ("manual_items", "combination_risks", "total", *(n for n, _ in _STR)):
            batch.drop_column(name)
