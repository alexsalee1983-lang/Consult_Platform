"""二期 B11：登记知识审核 Agent kb-reviewer（知识侧）。

不授权任何工具（二期开发计划第十二节第二级）：平台在知识条目提交审核后，把条目正文、引用到的依据原文和相近条目
直接交给它审查（app/kb/agent_review.py），它只返回审查意见，不经工具读写任何区域；发布与否由平台按规则决定。

Revision ID: 0036_kb_reviewer_agent
Revises: 0033_conversation_handoff
Create Date: 2026-11-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_kb_reviewer_agent"
down_revision: str | None = "0033_conversation_handoff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    agents = sa.table("agents", sa.column("code", sa.String), sa.column("name", sa.String),
                      sa.column("side", sa.String), sa.column("phase", sa.String), sa.column("enabled", sa.Boolean))
    op.bulk_insert(agents, [{"code": "kb-reviewer", "name": "知识审核", "side": "knowledge", "phase": "二期",
                             "enabled": True}])


def downgrade() -> None:
    op.execute("DELETE FROM agents WHERE code = 'kb-reviewer'")
