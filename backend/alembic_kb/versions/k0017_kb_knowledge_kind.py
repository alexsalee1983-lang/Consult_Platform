"""三期 B35 最小版：知识条目加知识类型 knowledge_kind 和章节槽位 slot。

- knowledge_kind：""（通用）/ practice（公司做法）/ golden（黄金样例）；后两类必须由作者以外的知识专家确认才能发布。
- slot：C10 章节槽位键，选填。
- 回填：GLD 条目为黄金样例；模板、方法、案例、常见坑、制度母版、异议应答、经验数值类条目，最新版本来源非公开的为公司做法
  （与 app/kb/meta.py 的 derive_kind 一致）。已发布的版本不动，只影响之后的发布。
- down 暂接 k0009（三期知识区链 k0010～k0016 在其他未合并 PR 里，合并时按第七节顺序改接）。

Revision ID: k0017_kb_knowledge_kind
Revises: k0009_kb_upload_project
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0017_kb_knowledge_kind"
down_revision: str | None = "k0009_kb_upload_project"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRACTICE_TYPES = ("TPL", "MTD", "CAS", "PIT", "POL", "OBJ", "BMK")


def upgrade() -> None:
    with op.batch_alter_table("kb_items") as batch:
        batch.add_column(sa.Column("knowledge_kind", sa.String(16), nullable=False, server_default=""))
        batch.add_column(sa.Column("slot", sa.String(64), nullable=False, server_default=""))
        batch.create_index("ix_kb_items_knowledge_kind", ["knowledge_kind"])
    op.execute("UPDATE kb_items SET knowledge_kind = 'golden' WHERE doc_type = 'GLD'")
    types = ", ".join(f"'{t}'" for t in PRACTICE_TYPES)
    op.execute(
        "UPDATE kb_items SET knowledge_kind = 'practice' WHERE doc_type IN (" + types + ") AND EXISTS ("
        "SELECT 1 FROM kb_versions v WHERE v.item_id = kb_items.id AND v.source_type <> 'public' AND v.version = ("
        "SELECT MAX(v2.version) FROM kb_versions v2 WHERE v2.item_id = kb_items.id))")


def downgrade() -> None:
    with op.batch_alter_table("kb_items") as batch:
        batch.drop_index("ix_kb_items_knowledge_kind")
        batch.drop_column("slot")
        batch.drop_column("knowledge_kind")
