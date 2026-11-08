"""M2：中文全文检索。启用 zhparser 扩展，建全文检索配置 zh（名词、动词、形容词、成语、简称、习用语等按原词索引）。

需要平台数据库镜像（deploy/postgres，含 zhparser）。SQLite（本地快速测试）没有全文检索，跳过。

Revision ID: 0009_zh_search
Revises: 0008_user_admin
Create Date: 2026-11-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_zh_search"
down_revision: str | None = "0008_user_admin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# zhparser 词性代号：n 名词、v 动词、a 形容词、i 成语、e 叹词、l 习用语、j 简称、x 未知词（英文缩写、编号等）
TOKEN_TYPES = "n,v,a,i,e,l,j,x"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    available = bind.execute(sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'zhparser'")).first()
    if not available:
        raise RuntimeError("数据库缺少 zhparser 扩展：请用平台数据库镜像（deploy/postgres）重建 postgres 服务，"
                           "执行 docker compose up -d --build")
    op.execute("CREATE EXTENSION IF NOT EXISTS zhparser")
    exists = bind.execute(sa.text("SELECT 1 FROM pg_ts_config WHERE cfgname = 'zh'")).first()
    if not exists:
        op.execute("CREATE TEXT SEARCH CONFIGURATION zh (PARSER = zhparser)")
        op.execute(f"ALTER TEXT SEARCH CONFIGURATION zh ADD MAPPING FOR {TOKEN_TYPES} WITH simple")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TEXT SEARCH CONFIGURATION IF EXISTS zh")
    op.execute("DROP EXTENSION IF EXISTS zhparser")
