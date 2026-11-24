"""四期 G3②：运行层配置改动版本与键值（runtime_config_versions / runtime_config_items）。

Revision ID: 0050_runtime_config
Revises: 0051_quality_incidents
Create Date: 2026-11-24

四期预留段 0050～0053（三期开发计划第七节）；0051 已给 F3（PR #371，先合并），本迁移接在 0051 之后（号不连续）。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050_runtime_config"
down_revision: str | None = "0051_quality_incidents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_config_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("needs_confirm", sa.Boolean(), nullable=False),
        sa.Column("rollback_to", sa.Integer(), nullable=True),
        sa.Column("requested_by", sa.String(64), nullable=False),
        sa.Column("decided_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_runtime_config_versions_status", "runtime_config_versions", ["status"])
    op.create_table(
        "runtime_config_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version_id", sa.Integer(), sa.ForeignKey("runtime_config_versions.id"), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("old_value", sa.JSON(), nullable=True),
    )
    op.create_index("ix_runtime_config_items_version_id", "runtime_config_items", ["version_id"])


def downgrade() -> None:
    op.drop_index("ix_runtime_config_items_version_id", table_name="runtime_config_items")
    op.drop_table("runtime_config_items")
    op.drop_index("ix_runtime_config_versions_status", table_name="runtime_config_versions")
    op.drop_table("runtime_config_versions")
