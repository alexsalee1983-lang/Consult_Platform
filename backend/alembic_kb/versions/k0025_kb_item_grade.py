"""四期 B43：条目分级——kb_items 加 grade（candidate 候选 / usable 可用 / recommended 推荐，app/kb/grade.py）。

已有的、有现行版本的条目都经过审核发布，回填为“可用”；未经专家认可的 AI 基线由每日分级扫描压回“候选”。

Revision ID: k0025_kb_item_grade
Revises: k0023_kb_expert_assign
Create Date: 2026-11-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0025_kb_item_grade"
down_revision: str | None = "k0023_kb_expert_assign"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_items") as batch:
        batch.add_column(sa.Column("grade", sa.String(16), nullable=False, server_default="candidate"))
        batch.create_index("ix_kb_items_grade", ["grade"])
    op.execute("UPDATE kb_items SET grade = 'usable' WHERE current_version_id IS NOT NULL")


def downgrade() -> None:
    conn = op.get_bind()
    views: list[str] = []
    if conn.dialect.name == "sqlite":   # SQLite 删列要重建 kb_items，先摘下引用它的视图（同 k0021），建完再挂回
        views = [r[0] for r in conn.execute(sa.text(
            "SELECT sql FROM sqlite_master WHERE type = 'view' AND sql LIKE '%kb_items%'"))]
        op.execute("DROP VIEW IF EXISTS kb_v_item_facets")
    with op.batch_alter_table("kb_items") as batch:
        batch.drop_index("ix_kb_items_grade")
        batch.drop_column("grade")
    for sql in views:
        op.execute(sql)
