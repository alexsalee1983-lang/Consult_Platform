"""二期 P16：投料加来源项目 source_project（默认空）。

项目沉淀投料时由项目成员选填来源项目代号，删除确认页据此预填经验回流的投料编号，负责人只需确认。
主库与知识区分库，只存代号、不建外键。

Revision ID: k0009_kb_upload_project
Revises: k0008_kb_control_ids
Create Date: 2026-11-18
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0009_kb_upload_project"
down_revision: str | None = "k0008_kb_control_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_uploads") as batch:
        batch.add_column(sa.Column("source_project", sa.String(32), nullable=False, server_default=""))
        batch.create_index("ix_kb_uploads_source_project", ["source_project"])


def downgrade() -> None:
    with op.batch_alter_table("kb_uploads") as batch:
        batch.drop_index("ix_kb_uploads_source_project")
        batch.drop_column("source_project")
