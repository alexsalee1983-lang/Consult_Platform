"""M4 ②：项目资料删除与删除记录（project_deletions），项目“客户不同意回流”标记和删除时间。

决策记录 2026-11-13：到期删除由平台规则自动发起、项目负责人确认；客户书面要求提前删除由项目经理发起、
项目负责人确认（两人不能是同一人，10 个工作日内完成）；已签署交付件一起删；确认时必须选择经验回流情况。
project_deletions 只追加状态，删除完成后就是可交给客户的删除记录（只记编号、日期、计数和确认人）。

Revision ID: 0025_project_deletion
Revises: 0024_kb_single_review
Create Date: 2026-11-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_project_deletion"
down_revision: str | None = "0024_kb_single_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("flowback_allowed", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "project_deletions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False, index=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("requested_by", sa.String(64), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_ref", sa.String(64), nullable=False, server_default=""),
        sa.Column("received_on", sa.Date(), nullable=True),
        sa.Column("due_by", sa.Date(), nullable=False),
        sa.Column("confirmed_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("flowback", sa.String(16), nullable=False, server_default=""),
        sa.Column("flowback_refs", sa.JSON(), nullable=False),
        sa.Column("cancelled_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("cancel_reason", sa.String(200), nullable=False, server_default=""),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("project_deletions")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("deleted_at")
        batch.drop_column("flowback_allowed")
