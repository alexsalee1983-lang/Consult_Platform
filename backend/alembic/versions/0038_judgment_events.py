"""二期 4A 埋点：projects.org_type（组织类型）、agent_runs.config_sha（配置哈希）、judgment_events（判断数据，只记计数和类别）。

Revision ID: 0038_judgment_events
Revises: 0037_user_enabled_at
Create Date: 2026-11-18
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_judgment_events"
down_revision: str | None = "0037_user_enabled_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as b:
        b.add_column(sa.Column("org_type", sa.String(16), nullable=False, server_default=""))
    with op.batch_alter_table("agent_runs") as b:
        b.add_column(sa.Column("config_sha", sa.String(64), nullable=False, server_default=""))
    op.create_table(
        "judgment_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("deliverable_code", sa.String(8), nullable=False),
        sa.Column("section_seq", sa.Integer(), nullable=True),
        sa.Column("agent_code", sa.String(32), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("magnitude", sa.Float(), nullable=True),
        sa.Column("attrs", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_judgment_events_project_id", "judgment_events", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_judgment_events_project_id", table_name="judgment_events")
    op.drop_table("judgment_events")
    with op.batch_alter_table("agent_runs") as b:
        b.drop_column("config_sha")
    with op.batch_alter_table("projects") as b:
        b.drop_column("org_type")
