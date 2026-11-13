"""M4 ④：严格档客户同意记录与去标识摘要卡（project_consents、summary_cards）。

严格档资料不出客户现场；客户书面同意（《数据处理计划》第四节）后，离场后只上传去标识摘要卡，Agent 用占位符起草。

Revision ID: 0027_consent_summary_cards
Revises: 0026_document_local_only
Create Date: 2026-11-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_consent_summary_cards"
down_revision: str | None = "0026_document_local_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_consents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("doc_ref", sa.String(64), nullable=False),
        sa.Column("signed_on", sa.Date(), nullable=False),
        sa.Column("recorded_by", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revoke_ref", sa.String(64), nullable=False, server_default=""),
        sa.Column("revoked_on", sa.Date(), nullable=True),
        sa.Column("revoked_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_project_consents_project_id", "project_consents", ["project_id"])
    op.create_table(
        "summary_cards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("consent_id", sa.Integer(), sa.ForeignKey("project_consents.id"), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("item", sa.String(200), nullable=False),
        sa.Column("finding", sa.Text(), nullable=False),
        sa.Column("magnitude", sa.String(100), nullable=False),
        sa.Column("onsite_refs", sa.JSON(), nullable=False),
        sa.Column("client_reviewed_on", sa.Date(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("withdrawn", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("withdrawn_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("project_id", "code", name="uq_summary_cards_code"),
    )
    op.create_index("ix_summary_cards_project_id", "summary_cards", ["project_id"])
    op.create_index("ix_summary_cards_consent_id", "summary_cards", ["consent_id"])


def downgrade() -> None:
    op.drop_index("ix_summary_cards_consent_id", "summary_cards")
    op.drop_index("ix_summary_cards_project_id", "summary_cards")
    op.drop_table("summary_cards")
    op.drop_index("ix_project_consents_project_id", "project_consents")
    op.drop_table("project_consents")
