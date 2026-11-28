"""四期 A30：项目定制项（D1 客户要求栏、顾问对话、编辑器三入口汇成一张表）。

customizations：入口、类别（形式 / 口径 / 结构）、结构类的增删动作与章节标题、适用交付件与章、是否一次性、
定制项正文（已过机密级检测）、状态、不可接受理由、关联确认卡、确认人与生效（或生效后撤回）时间。

Revision ID: 0059_customizations
Revises: 0058_selfeval_pack
Create Date: 2026-11-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059_customizations"
down_revision: str | None = "0058_selfeval_pack"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customizations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("action", sa.String(8), server_default="", nullable=False),
        sa.Column("section_title", sa.String(128), server_default="", nullable=False),
        sa.Column("deliverable_code", sa.String(8), server_default="", nullable=False),
        sa.Column("section_seq", sa.Integer(), nullable=True),
        sa.Column("one_time", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(256), server_default="", nullable=False),
        sa.Column("card_id", sa.Integer(), sa.ForeignKey("confirmations.id"), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(64), server_default="", nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_customizations_project_id", "customizations", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_customizations_project_id", table_name="customizations")
    op.drop_table("customizations")
