"""四期 F3②e：Agent md 版本库 agent_md_versions 与回滚指针 agent_md_pins。

Revision ID: 0052_agent_md_versions
Revises: 0043_task_returns（main 链头）
Create Date: 2026-11-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052_agent_md_versions"
down_revision: str | None = "0043_task_returns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_md_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_code", sa.String(32), nullable=False),
        sa.Column("version", sa.String(12), nullable=False),
        sa.Column("md_version", sa.String(32), nullable=False),
        sa.Column("common_text", sa.Text(), nullable=False),
        sa.Column("role_text", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("agent_code", "version", name="uq_agent_md_versions_code_version"),
    )
    op.create_index("ix_agent_md_versions_agent_code", "agent_md_versions", ["agent_code"])
    op.create_table(
        "agent_md_pins",
        sa.Column("agent_code", sa.String(32), primary_key=True),
        sa.Column("version", sa.String(12), nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=True),
        sa.Column("pinned_by", sa.String(64), nullable=False),
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("agent_md_pins")
    op.drop_index("ix_agent_md_versions_agent_code", table_name="agent_md_versions")
    op.drop_table("agent_md_versions")
