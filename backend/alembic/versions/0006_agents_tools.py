"""M1：Agent、工具与 Agent × 工具授权（agents、tools、agent_tool_grants）。

授权清单照搬《架构设计》第五节“Agent × 工具授权”表；方案设计 Agent 按决策记录新增
（agents/roles/solution-designer.md，tools 字段为知识检索、依据查询、读项目区，与本表一致）；
新人带教、售前方案为预留 Agent，登记但停用、不授予任何工具。
以后调整授权：新写一个迁移，在 PR 里说明依据，并同步 tests/test_tools.py 的授权矩阵。

Revision ID: 0006_agents_tools
Revises: 0005_llm_usage
Create Date: 2026-11-08
"""
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "0006_agents_tools"
down_revision: str | None = "0005_llm_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AGENTS = [  # code, 名称, 侧, 阶段, 启用
    ("consultant-lead", "咨询师", "delivery", "一期", True),
    ("delivery-pm", "项目交付管理", "delivery", "一期", True),
    ("research-interview", "调研访谈", "delivery", "一期", True),
    ("compliance-basis", "依据与差距分析", "delivery", "一期", True),
    ("solution-designer", "方案设计", "delivery", "一期", True),
    ("policy-drafter", "制度起草", "delivery", "一期", True),
    ("report-writer", "报告与汇报", "delivery", "一期", True),
    ("qa-reviewer", "质量评审", "delivery", "一期", True),
    ("kb-builder", "知识库建设", "knowledge", "一期", True),
    ("sanitizer", "脱敏审查", "knowledge", "一期", True),
    ("coach", "新人带教", "knowledge", "预留", False),
    ("presales", "售前方案", "knowledge", "预留", False),
]
TOOLS = [  # code, 名称, 区域, 读写
    ("kb.search", "知识检索", "knowledge", "read"),
    ("basis.lookup", "依据查询", "knowledge", "read"),
    ("project.read", "读项目区", "project", "read"),
    ("doc.generate", "文档生成", "project", "write"),
    ("quality.check", "质量检查", "none", "read"),
    ("kb.stage_write", "写待入库区", "staging", "write"),
]
GRANTS = {
    "consultant-lead": ["kb.search", "project.read"],
    "delivery-pm": ["kb.search", "project.read", "doc.generate"],
    "research-interview": ["kb.search", "project.read", "doc.generate"],
    "compliance-basis": ["kb.search", "basis.lookup", "project.read"],
    "solution-designer": ["kb.search", "basis.lookup", "project.read"],
    "policy-drafter": ["kb.search", "basis.lookup", "project.read", "doc.generate"],
    "report-writer": ["kb.search", "project.read", "doc.generate"],
    "qa-reviewer": ["kb.search", "basis.lookup", "project.read", "quality.check"],
    "kb-builder": ["kb.search", "basis.lookup", "kb.stage_write"],
    "sanitizer": ["kb.stage_write"],
}


def upgrade() -> None:
    agents = op.create_table(
        "agents",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("code"),
    )
    tools = op.create_table(
        "tools",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("zone", sa.String(length=16), nullable=False),
        sa.Column("access", sa.String(length=8), nullable=False),
        sa.PrimaryKeyConstraint("code"),
    )
    grants = op.create_table(
        "agent_tool_grants",
        sa.Column("agent_code", sa.String(length=32), nullable=False),
        sa.Column("tool_code", sa.String(length=32), nullable=False),
        sa.Column("granted_by", sa.String(length=64), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_code"], ["agents.code"]),
        sa.ForeignKeyConstraint(["tool_code"], ["tools.code"]),
        sa.PrimaryKeyConstraint("agent_code", "tool_code"),
    )
    op.bulk_insert(agents, [{"code": c, "name": n, "side": s, "phase": p, "enabled": e} for c, n, s, p, e in AGENTS])
    op.bulk_insert(tools, [{"code": c, "name": n, "zone": z, "access": a} for c, n, z, a in TOOLS])
    now = datetime.now(timezone.utc)
    op.bulk_insert(grants, [{"agent_code": a, "tool_code": t, "granted_by": "migration:0006", "granted_at": now}
                            for a, ts in GRANTS.items() for t in ts])


def downgrade() -> None:
    op.drop_table("agent_tool_grants")
    op.drop_table("tools")
    op.drop_table("agents")
