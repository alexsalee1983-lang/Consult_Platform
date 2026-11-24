"""四期 B28①：多语言与海外法规最小版——知识条目加原文语种 language、辖区 jurisdiction、译文挂的原文编号 translation_of、
“非官方译本”标记 translation_notice（app/kb/multilang.py）。旧条目回填 zh / CN / 空。

编号按三期计划第七节续号，接知识区链头 k0016（#433 登记）。

Revision ID: k0021_kb_multilang
Revises: k0016_kb_version_model
Create Date: 2026-11-25
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0021_kb_multilang"
down_revision: str | None = "k0016_kb_version_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_items") as batch:
        batch.add_column(sa.Column("language", sa.String(16), nullable=False, server_default="zh"))
        batch.add_column(sa.Column("jurisdiction", sa.String(16), nullable=False, server_default="CN"))
        batch.add_column(sa.Column("translation_of", sa.String(40), nullable=False, server_default=""))
        batch.add_column(sa.Column("translation_notice", sa.String(16), nullable=False, server_default=""))
        batch.create_index("ix_kb_items_jurisdiction", ["jurisdiction"])
        batch.create_index("ix_kb_items_translation_of", ["translation_of"])


def downgrade() -> None:
    conn = op.get_bind()
    views: list[str] = []
    if conn.dialect.name == "sqlite":   # SQLite 删列要重建 kb_items，先摘下引用它的视图（同 k0017），建完再挂回
        views = [r[0] for r in conn.execute(sa.text(
            "SELECT sql FROM sqlite_master WHERE type = 'view' AND sql LIKE '%kb_items%'"))]
        op.execute("DROP VIEW IF EXISTS kb_v_item_facets")
    with op.batch_alter_table("kb_items") as batch:
        batch.drop_index("ix_kb_items_translation_of")
        batch.drop_index("ix_kb_items_jurisdiction")
        batch.drop_column("translation_notice")
        batch.drop_column("translation_of")
        batch.drop_column("jurisdiction")
        batch.drop_column("language")
    for sql in views:
        op.execute(sql)
