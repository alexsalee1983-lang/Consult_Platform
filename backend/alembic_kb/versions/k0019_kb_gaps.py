"""三期 B25②：知识缺口池 kb_gaps。

五类触发源的信号按 gap_key 合并，每周排计划（直接采集 / 来源确认 / 顺延 / 累积），记关闭原因与时长，供 F2 看板。
只存编号与计数，不存原文。迁移号 k0019 已在三期开发计划第七节表登记；基于 B16（k0014），
B15 链、B19、B17 等合并后由后合并的一方改接。

Revision ID: k0019_kb_gaps
Revises: k0020_kb_reg_lifecycle
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0019_kb_gaps"
down_revision: str | None = "k0020_kb_reg_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kb_gaps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("gap_key", sa.String(500), nullable=False),
        sa.Column("kinds", sa.JSON(), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("impact", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("due", sa.Date(), nullable=True),
        sa.Column("authority", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("status", sa.String(12), nullable=False, server_default="open"),
        sa.Column("plan", sa.String(12), nullable=False, server_default=""),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(16), nullable=False, server_default=""),
        sa.Column("reopened", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("gap_key", name="uq_kb_gaps_gap_key"),
    )
    op.create_index("ix_kb_gaps_status", "kb_gaps", ["status"])


def downgrade() -> None:
    op.drop_index("ix_kb_gaps_status", table_name="kb_gaps")
    op.drop_table("kb_gaps")
