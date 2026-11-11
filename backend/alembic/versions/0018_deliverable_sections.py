"""M3：交付件分章节与版本（deliverable_sections、section_versions），deliverables 加 revision。

“文档生成”doc.generate 由占位实现改为真实实现（写大纲、写章节、读章节），授权不变（迁移 0006：
项目交付管理、调研访谈、制度起草、报告与汇报 4 个 Agent）。

Revision ID: 0018_deliverable_sections
Revises: 0017_project_evidence
Create Date: 2026-11-11
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_deliverable_sections"
down_revision: str | None = "0017_project_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("deliverables") as batch:
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("updated_by", sa.String(length=64), nullable=False, server_default=""))
    op.create_table(
        "deliverable_sections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deliverable_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["deliverable_id"], ["deliverables.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deliverable_id", "seq", name="uq_deliverable_sections_seq"),
    )
    op.create_index("ix_deliverable_sections_deliverable_id", "deliverable_sections", ["deliverable_id"])
    op.create_table(
        "section_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("section_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("body_sha", sa.String(length=64), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=False),
        sa.Column("author", sa.String(length=64), nullable=False),
        sa.Column("agent_code", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["section_id"], ["deliverable_sections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("section_id", "version", name="uq_section_versions_version"),
    )
    op.create_index("ix_section_versions_section_id", "section_versions", ["section_id"])


def downgrade() -> None:
    op.drop_index("ix_section_versions_section_id", table_name="section_versions")
    op.drop_table("section_versions")
    op.drop_index("ix_deliverable_sections_deliverable_id", table_name="deliverable_sections")
    op.drop_table("deliverable_sections")
    with op.batch_alter_table("deliverables") as batch:
        batch.drop_column("updated_by")
        batch.drop_column("updated_at")
        batch.drop_column("revision")
