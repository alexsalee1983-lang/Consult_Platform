"""三期 B16：来源登记表 kb_sources 与状态变更记录 kb_source_events。

采集机只按试用、正式状态的来源抓取；许可、权威类别必填，等级由类别推出（B27 最小版三级）。
迁移号按三期开发计划第七节表；down 暂接 k0009（B15 链 k0010～k0012、B19 k0013 合并后由后合并的一方改接）。

Revision ID: k0014_kb_sources
Revises: k0009_kb_upload_project
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0014_kb_sources"
down_revision: str | None = "k0009_kb_upload_project"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kb_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("host", sa.String(253), nullable=False),
        sa.Column("publisher", sa.String(200), nullable=False, server_default=""),
        sa.Column("license", sa.String(300), nullable=False),
        sa.Column("authority_kind", sa.String(16), nullable=False),
        sa.Column("authority_level", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="candidate"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("approved_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("url", name="uq_kb_sources_url"),
    )
    op.create_index("ix_kb_sources_code", "kb_sources", ["code"], unique=True)
    op.create_index("ix_kb_sources_host", "kb_sources", ["host"])
    op.create_index("ix_kb_sources_authority_level", "kb_sources", ["authority_level"])
    op.create_index("ix_kb_sources_status", "kb_sources", ["status"])
    op.create_table(
        "kb_source_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("kb_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_status", sa.String(12), nullable=False, server_default=""),
        sa.Column("to_status", sa.String(12), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False, server_default=""),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_kb_source_events_source_id", "kb_source_events", ["source_id"])


def downgrade() -> None:
    op.drop_index("ix_kb_source_events_source_id", table_name="kb_source_events")
    op.drop_table("kb_source_events")
    for ix in ("status", "authority_level", "host", "code"):
        op.drop_index(f"ix_kb_sources_{ix}", table_name="kb_sources")
    op.drop_table("kb_sources")
