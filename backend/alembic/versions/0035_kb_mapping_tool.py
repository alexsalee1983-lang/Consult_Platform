"""二期 B3：新工具“控制映射查询” kb.mapping（知识区只读）及授权。

授权（docs/接口约定.md 第六节）：依据与差距分析 compliance-basis、方案设计 solution-designer、制度起草 policy-drafter、
咨询师 consultant-lead；知识侧 Agent 不需要。映射表 kb_mappings 在知识区迁移链 k0003。

Revision ID: 0035_kb_mapping_tool
Revises: 0033_conversation_handoff
Create Date: 2026-11-15
"""
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "0035_kb_mapping_tool"
down_revision: str | None = "0033_conversation_handoff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AGENTS = ("compliance-basis", "solution-designer", "policy-drafter", "consultant-lead")


def upgrade() -> None:
    tools = sa.table("tools", sa.column("code", sa.String), sa.column("name", sa.String),
                     sa.column("zone", sa.String), sa.column("access", sa.String))
    op.bulk_insert(tools, [{"code": "kb.mapping", "name": "控制映射查询", "zone": "knowledge", "access": "read"}])
    grants = sa.table("agent_tool_grants", sa.column("agent_code", sa.String), sa.column("tool_code", sa.String),
                      sa.column("granted_by", sa.String), sa.column("granted_at", sa.DateTime(timezone=True)))
    now = datetime.now(timezone.utc)
    op.bulk_insert(grants, [{"agent_code": a, "tool_code": "kb.mapping", "granted_by": "migration:0035",
                             "granted_at": now} for a in AGENTS])


def downgrade() -> None:
    op.execute("DELETE FROM agent_tool_grants WHERE tool_code = 'kb.mapping'")
    op.execute("DELETE FROM tools WHERE code = 'kb.mapping'")
