"""四期 B25：缺口池“需人工下载”——kb_gaps 加 manual_reason（原因类别，见 app/kb/acquisition.py MANUAL_REASONS）。

plan 新增取值 manual（需人工下载）、uploaded（已上传、后台处理中），字段长度不变。

Revision ID: k0022_kb_gap_manual
Revises: k0021_kb_multilang
Create Date: 2026-11-25
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0022_kb_gap_manual"
down_revision: str | None = "k0021_kb_multilang"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_gaps") as batch:
        batch.add_column(sa.Column("manual_reason", sa.String(16), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("kb_gaps") as batch:
        batch.drop_column("manual_reason")
