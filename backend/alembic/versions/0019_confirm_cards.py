"""M3：确认卡扩展（confirmations 加选项、推荐、理由、影响、关联交付件、处理记录），新工具“发确认卡”confirm.create。

授权（决策记录 2026-11-10“新增两个工具的授权”）：confirm.create 是项目区写工具，授权给全部 8 个交付侧 Agent——
发卡只是停下来问顾问，不写客户资料、不碰知识区；知识侧 Agent 不授权，硬规则也会拒绝。

Revision ID: 0019_confirm_cards
Revises: 0018_deliverable_sections
Create Date: 2026-11-11
"""
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "0019_confirm_cards"
down_revision: str | None = "0018_deliverable_sections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GRANTED_TO = ["consultant-lead", "delivery-pm", "research-interview", "compliance-basis", "solution-designer",
              "policy-drafter", "report-writer", "qa-reviewer"]
TEXT_COLUMNS = [("agent_code", 32), ("recommendation", 8), ("deliverable_code", 8), ("created_by", 64), ("choice", 8),
                ("resolved_by", 64)]


def upgrade() -> None:
    with op.batch_alter_table("confirmations") as batch:
        for name, length in TEXT_COLUMNS:
            batch.add_column(sa.Column(name, sa.String(length=length), nullable=False, server_default=""))
        batch.add_column(sa.Column("options", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("reason", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("impact", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("section_seq", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))

    tools = sa.table("tools", sa.column("code", sa.String), sa.column("name", sa.String),
                     sa.column("zone", sa.String), sa.column("access", sa.String))
    op.bulk_insert(tools, [{"code": "confirm.create", "name": "发确认卡", "zone": "project", "access": "write"}])
    grants = sa.table("agent_tool_grants", sa.column("agent_code", sa.String), sa.column("tool_code", sa.String),
                      sa.column("granted_by", sa.String), sa.column("granted_at", sa.DateTime(timezone=True)))
    now = datetime.now(timezone.utc)
    op.bulk_insert(grants, [{"agent_code": a, "tool_code": "confirm.create", "granted_by": "migration:0019",
                             "granted_at": now} for a in GRANTED_TO])


def downgrade() -> None:
    op.execute("DELETE FROM agent_tool_grants WHERE tool_code = 'confirm.create'")
    op.execute("DELETE FROM tools WHERE code = 'confirm.create'")
    with op.batch_alter_table("confirmations") as batch:
        for name in ("resolved_at", "section_seq", "impact", "reason", "options"):
            batch.drop_column(name)
        for name, _ in reversed(TEXT_COLUMNS):
            batch.drop_column(name)
