"""三期 C6-P：Agent 名册按 9 个——登记咨询协调 consult-coordinator、知识采集 kb-collector 及授权。

- 咨询协调（C6①，agents/roles/consult-coordinator.md，replaces consultant-lead、delivery-pm）：交付侧，
  授权取两个旧 Agent 的并集，与 md 的 tools 一致：知识检索、读项目区（含查证据、发确认卡）、控制映射查询、
  文档生成、转交 Agent。旧代号本迁移不停用，C6 合并步骤经负责人确认后另写迁移停用，届时旧代号自动转到新代号
  （app/agents/roster.py）。
- 知识采集（C11，agents/roles/kb-collector.md）：所在区为采集机区（side = collector），不授权任何平台工具，
  硬规则另在 app/agents/tools.py 拦截（采集机区 Agent 不能使用平台工具）。
- 主链编号 0045 原留给 A15（A15 不需表，空号；三期开发计划第七节），开发线 3 改用于本项。

Revision ID: 0045_nine_agents
Revises: 0052_agent_md_versions（main 链头；down_revision 合并时接当时链头，决策记录 2026-11-23）
Create Date: 2026-11-24
"""
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "0045_nine_agents"
down_revision: str | None = "0052_agent_md_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AGENTS = [  # code, 名称, 侧, 阶段, 启用
    ("consult-coordinator", "咨询协调", "delivery", "三期", True),
    ("kb-collector", "知识采集", "collector", "三期", True),
]
GRANTS = {
    "consult-coordinator": ["kb.search", "project.read", "project.evidence", "confirm.create", "kb.mapping",
                            "doc.generate", "agent.handoff"],
}


def upgrade() -> None:
    agents = sa.table("agents", sa.column("code", sa.String), sa.column("name", sa.String),
                      sa.column("side", sa.String), sa.column("phase", sa.String), sa.column("enabled", sa.Boolean))
    op.bulk_insert(agents, [{"code": c, "name": n, "side": s, "phase": p, "enabled": e} for c, n, s, p, e in AGENTS])
    grants = sa.table("agent_tool_grants", sa.column("agent_code", sa.String), sa.column("tool_code", sa.String),
                      sa.column("granted_by", sa.String), sa.column("granted_at", sa.DateTime(timezone=True)))
    now = datetime.now(timezone.utc)
    op.bulk_insert(grants, [{"agent_code": a, "tool_code": t, "granted_by": "migration:0045", "granted_at": now}
                            for a, ts in GRANTS.items() for t in ts])


def downgrade() -> None:
    op.execute("DELETE FROM agent_tool_grants WHERE agent_code IN ('consult-coordinator', 'kb-collector')")
    op.execute("DELETE FROM agents WHERE code IN ('consult-coordinator', 'kb-collector')")
