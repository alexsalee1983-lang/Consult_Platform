"""四期 A32：立项与合同要点卡、授权台账、先行启动。

- contract_cards：合同要点卡（每版一行，要点存 JSON：value / state / source）；
- project_authorizations：授权台账（只记签署状态，追加式）；
- projects.intake / early_start_on / early_start_approved_by / early_start_approved_at。

Revision ID: 0061_contract_card
Revises: 0060_delivery_loops
Create Date: 2026-11-28
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0061_contract_card"
down_revision: str | None = "0060_delivery_loops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contract_cards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("provisional", sa.Boolean(), nullable=False),
        sa.Column("basis_kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(200), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_by", sa.String(64), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("project_id", "version", name="uq_contract_card_version"),
    )
    op.create_index("ix_contract_cards_project_id", "contract_cards", ["project_id"])
    op.create_table(
        "project_authorizations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("signed_on", sa.Date(), nullable=False),
        sa.Column("recorded_by", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_project_authorizations_project_id", "project_authorizations", ["project_id"])
    with op.batch_alter_table("projects") as b:
        b.add_column(sa.Column("intake", sa.Boolean(), server_default=sa.false(), nullable=False))
        b.add_column(sa.Column("early_start_on", sa.Date(), nullable=True))
        b.add_column(sa.Column("early_start_approved_by", sa.String(64), server_default="", nullable=False))
        b.add_column(sa.Column("early_start_approved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("projects") as b:
        b.drop_column("early_start_approved_at")
        b.drop_column("early_start_approved_by")
        b.drop_column("early_start_on")
        b.drop_column("intake")
    op.drop_index("ix_project_authorizations_project_id", table_name="project_authorizations")
    op.drop_table("project_authorizations")
    op.drop_index("ix_contract_cards_project_id", table_name="contract_cards")
    op.drop_table("contract_cards")
