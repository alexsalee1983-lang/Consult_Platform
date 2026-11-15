"""M5：project_documents.text_sha256（正文哈希，另存或换格式的同一份资料也识别为重复；仅现场使用的资料不能借此再上传）。

迁移前上传的资料没有正文哈希（空串）：仅现场使用的资料原文已删，无法补算。

Revision ID: 0031_document_text_sha
Revises: 0030_kb_split
Create Date: 2026-11-14
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_document_text_sha"
down_revision: str | None = "0030_kb_split"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("project_documents") as b:
        b.add_column(sa.Column("text_sha256", sa.String(64), server_default="", nullable=False))
        b.create_index("ix_project_documents_text_sha256", ["text_sha256"])


def downgrade() -> None:
    with op.batch_alter_table("project_documents") as b:
        b.drop_index("ix_project_documents_text_sha256")
        b.drop_column("text_sha256")
