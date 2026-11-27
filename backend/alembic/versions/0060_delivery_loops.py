"""四期 A31：交付侧 12 个缺口闭环。

- projects.terminated_at / terminated_by：项目终止（停止自动推进、关闭待办，保留期从终止日起算）；
- confirmations.subject：平台卡关联的对象（evidence:D-03、run:12、deliverable:D4、section:D4:3），同一对象同类未处理卡不重发；
- confirmations.remind_stage：确认卡提醒阶段（0 / 1 三个工作日已提醒 / 2 七天已升级项目经理）；
- deliverable_sections.stale_reason / stale_at：上游变更后标“需更新”，写入新版本后清空。

Revision ID: 0060_delivery_loops
Revises: 0058_selfeval_pack
Create Date: 2026-11-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0060_delivery_loops"
down_revision: str | None = "0058_selfeval_pack"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as b:
        b.add_column(sa.Column("terminated_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("terminated_by", sa.String(64), server_default="", nullable=False))
    with op.batch_alter_table("confirmations") as b:
        b.add_column(sa.Column("subject", sa.String(64), server_default="", nullable=False))
        b.add_column(sa.Column("remind_stage", sa.Integer(), server_default="0", nullable=False))
    with op.batch_alter_table("deliverable_sections") as b:
        b.add_column(sa.Column("stale_reason", sa.String(64), server_default="", nullable=False))
        b.add_column(sa.Column("stale_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("deliverable_sections") as b:
        b.drop_column("stale_at")
        b.drop_column("stale_reason")
    with op.batch_alter_table("confirmations") as b:
        b.drop_column("remind_stage")
        b.drop_column("subject")
    with op.batch_alter_table("projects") as b:
        b.drop_column("terminated_by")
        b.drop_column("terminated_at")
