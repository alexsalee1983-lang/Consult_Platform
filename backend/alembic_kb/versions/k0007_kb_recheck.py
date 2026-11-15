"""二期 B13：知识版本加使用反馈待复查 recheck_state / recheck_reason / recheck_at（默认空）。

顾问在交付件里常删掉 Agent 引用的条目时，由平台标记待复查，进抽检队列；与自动发布抽检 sample_state 分开，
不计入自动发布的抽检不通过率。原因只写计数，不写交付件内容。

Revision ID: k0007_kb_recheck
Revises: k0006_kb_review_findings
Create Date: 2026-11-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0007_kb_recheck"
down_revision: str | None = "k0006_kb_review_findings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_versions") as batch:
        batch.add_column(sa.Column("recheck_state", sa.String(16), nullable=False, server_default=""))
        batch.add_column(sa.Column("recheck_reason", sa.String(200), nullable=False, server_default=""))
        batch.add_column(sa.Column("recheck_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_kb_versions_recheck_state", ["recheck_state"])


def downgrade() -> None:
    with op.batch_alter_table("kb_versions") as batch:
        batch.drop_index("ix_kb_versions_recheck_state")
        batch.drop_column("recheck_at")
        batch.drop_column("recheck_reason")
        batch.drop_column("recheck_state")
