"""二期 C2：对话转交。conversations 加 handoff_from（来源对话，可空）；新工具“转交 Agent”agent.handoff。

授权（决策记录 2026-11-14“对话由咨询师 Agent 转交专业 Agent”）：agent.handoff 是项目区写工具（在项目里新建对话），
只授权给咨询师 Agent consultant-lead——由它分派任务，专业 Agent 之间不互相转交；知识侧 Agent 不授权，硬规则也会拒绝。
转交只由平台在顾问点“转交”时调用（调试接口不能调写入类工具）。

Revision ID: 0033_conversation_handoff
Revises: 0032_project_configs
Create Date: 2026-11-14
"""
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "0033_conversation_handoff"
down_revision: str | None = "0032_project_configs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("handoff_from", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_conversations_handoff_from", "conversations", ["handoff_from"], ["id"])

    tools = sa.table("tools", sa.column("code", sa.String), sa.column("name", sa.String),
                     sa.column("zone", sa.String), sa.column("access", sa.String))
    op.bulk_insert(tools, [{"code": "agent.handoff", "name": "转交 Agent", "zone": "project", "access": "write"}])
    grants = sa.table("agent_tool_grants", sa.column("agent_code", sa.String), sa.column("tool_code", sa.String),
                      sa.column("granted_by", sa.String), sa.column("granted_at", sa.DateTime(timezone=True)))
    op.bulk_insert(grants, [{"agent_code": "consultant-lead", "tool_code": "agent.handoff",
                             "granted_by": "migration:0033", "granted_at": datetime.now(timezone.utc)}])


def downgrade() -> None:
    op.execute("DELETE FROM agent_tool_grants WHERE tool_code = 'agent.handoff'")
    op.execute("DELETE FROM tools WHERE code = 'agent.handoff'")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_constraint("fk_conversations_handoff_from", type_="foreignkey")
        batch.drop_column("handoff_from")
