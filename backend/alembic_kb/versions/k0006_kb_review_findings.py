"""二期 B11：审核记录加 findings（知识审核 Agent 的逐项检查和问题清单，JSON，人工审核记录为空列表）。

内容是对去标识后公开资料条目的审查意见，只存知识区库、不进审计。

Revision ID: k0006_kb_review_findings
Revises: k0005_kb_auto_review
Create Date: 2026-11-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0006_kb_review_findings"
down_revision: str | None = "k0005_kb_auto_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_reviews") as batch:
        batch.add_column(sa.Column("findings", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    with op.batch_alter_table("kb_reviews") as batch:
        batch.drop_column("findings")
