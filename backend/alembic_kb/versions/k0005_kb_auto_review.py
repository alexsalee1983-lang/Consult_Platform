"""二期 B10：知识版本加自动发布标记 auto_review、抽检状态 sample_state（默认空）。

平台核验自动发布的依据原文 auto_review=platform；抽检 pending / passed / failed。已有版本一律为空（人工审核）。

Revision ID: k0005_kb_auto_review
Revises: k0004_kb_ref_code
Create Date: 2026-11-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0005_kb_auto_review"
down_revision: str | None = "k0004_kb_ref_code"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_versions") as batch:
        batch.add_column(sa.Column("auto_review", sa.String(16), nullable=False, server_default=""))
        batch.add_column(sa.Column("sample_state", sa.String(16), nullable=False, server_default=""))
        batch.create_index("ix_kb_versions_sample_state", ["sample_state"])


def downgrade() -> None:
    with op.batch_alter_table("kb_versions") as batch:
        batch.drop_index("ix_kb_versions_sample_state")
        batch.drop_column("sample_state")
        batch.drop_column("auto_review")
