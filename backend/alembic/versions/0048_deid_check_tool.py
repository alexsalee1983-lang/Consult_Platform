"""三期 C7②：新工具“去标识检查” deid.check（只检查不改数据）及授权。

接 app/kb/deid_check.py：机密级检测（guard，红线 3）命中即退回；残留直接标识为有条件通过；输出只有类别和计数，不含原文。
授权只给知识库建设 kb-builder（负责人 2026-11-23，#342；决策记录 #355）。区域 none、读取类，同 quality.check。

Revision ID: 0048_deid_check_tool
Revises: 0039_findings（main 链头；down_revision 合并时接当时链头，决策记录 2026-11-23）
Create Date: 2026-11-23
"""
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "0048_deid_check_tool"
down_revision: str | None = "0039_findings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AGENTS = ("kb-builder",)


def upgrade() -> None:
    tools = sa.table("tools", sa.column("code", sa.String), sa.column("name", sa.String),
                     sa.column("zone", sa.String), sa.column("access", sa.String))
    op.bulk_insert(tools, [{"code": "deid.check", "name": "去标识检查", "zone": "none", "access": "read"}])
    grants = sa.table("agent_tool_grants", sa.column("agent_code", sa.String), sa.column("tool_code", sa.String),
                      sa.column("granted_by", sa.String), sa.column("granted_at", sa.DateTime(timezone=True)))
    now = datetime.now(timezone.utc)
    op.bulk_insert(grants, [{"agent_code": a, "tool_code": "deid.check", "granted_by": "migration:0048",
                             "granted_at": now} for a in AGENTS])


def downgrade() -> None:
    op.execute("DELETE FROM agent_tool_grants WHERE tool_code = 'deid.check'")
    op.execute("DELETE FROM tools WHERE code = 'deid.check'")
