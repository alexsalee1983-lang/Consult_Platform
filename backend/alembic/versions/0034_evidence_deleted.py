"""二期：evidences.deleted_at / deleted_by（证据删除：编号保留不复用，内容清空）。

Revision ID: 0034_evidence_deleted
Revises: 0031_document_text_sha（暂接 #98；0032、0033 合并后改接 0033_conversation_handoff）
Create Date: 2026-11-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_evidence_deleted"
down_revision: str | None = "0031_document_text_sha"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("evidences") as b:
        b.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("deleted_by", sa.String(64), server_default="", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("evidences") as b:
        b.drop_column("deleted_by")
        b.drop_column("deleted_at")
