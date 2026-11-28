"""四期 B42：投毒防护六层与处置流程（app/kb/poison.py）。

- kb_versions.batch_key：投料批次（U<投料编号> / I-<日期>-<目录哈希> 批量导入 / M-<日期>-<用户名> 手工起草），
  与 created_by、submitted_by 一起做到每条知识可追责（第①层）；poison_flags 为命中的防护类别
  （instruction / outlier / poor_record），有值即不得单人发布、不交 Agent 自动发布（第③层）；
  single_source 为“单一来源”标记，检索时降权（第④层）。
- kb_items.derived_from：派生自哪个条目（解读、做法的母条目编号），一键清除时一起清除（第⑥层）；
  frozen_by：被哪个投毒事件冻结（冻结即下线，等待审批期间检索不到）。
- kb_poison_incidents：投毒事件（冻结 → 知识管理员提处置意见 → 咨询业务负责人批准后执行），说明只记长度和哈希。

Revision ID: k0024_kb_poison
Revises: k0026_kb_gap_loop
Create Date: 2026-11-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0024_kb_poison"
down_revision: str | None = "k0026_kb_gap_loop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_versions") as batch:
        batch.add_column(sa.Column("batch_key", sa.String(64), nullable=False, server_default=""))
        batch.add_column(sa.Column("poison_flags", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("single_source", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_index("ix_kb_versions_batch_key", ["batch_key"])
    with op.batch_alter_table("kb_items") as batch:
        batch.add_column(sa.Column("derived_from", sa.String(40), nullable=False, server_default=""))
        batch.add_column(sa.Column("frozen_by", sa.Integer(), nullable=True))
        batch.create_index("ix_kb_items_derived_from", ["derived_from"])
        batch.create_index("ix_kb_items_frozen_by", ["frozen_by"])
    op.create_table(
        "kb_poison_incidents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trigger", sa.String(24), nullable=False),
        sa.Column("scope_kind", sa.String(12), nullable=False, server_default=""),
        sa.Column("scope_value", sa.String(64), nullable=False, server_default=""),
        sa.Column("submitter", sa.String(64), nullable=False, server_default=""),
        sa.Column("categories", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="frozen"),
        sa.Column("frozen_items", sa.JSON(), nullable=True),
        sa.Column("affected", sa.JSON(), nullable=True),
        sa.Column("reported_by", sa.String(64), nullable=False),
        sa.Column("note_len", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note_sha", sa.String(16), nullable=False, server_default=""),
        sa.Column("proposal", sa.String(12), nullable=False, server_default=""),
        sa.Column("proposal_items", sa.JSON(), nullable=True),
        sa.Column("proposed_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("quality_incident_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_kb_poison_incidents_status", "kb_poison_incidents", ["status"])
    op.create_index("ix_kb_poison_incidents_submitter", "kb_poison_incidents", ["submitter"])


def downgrade() -> None:
    op.drop_index("ix_kb_poison_incidents_submitter", table_name="kb_poison_incidents")
    op.drop_index("ix_kb_poison_incidents_status", table_name="kb_poison_incidents")
    op.drop_table("kb_poison_incidents")
    conn = op.get_bind()
    views: list[str] = []
    if conn.dialect.name == "sqlite":   # SQLite 删列要重建 kb_items，先摘下引用它的视图（同 k0025），建完再挂回
        views = [r[0] for r in conn.execute(sa.text(
            "SELECT sql FROM sqlite_master WHERE type = 'view' AND sql LIKE '%kb_items%'"))]
        op.execute("DROP VIEW IF EXISTS kb_v_item_facets")
    with op.batch_alter_table("kb_items") as batch:
        batch.drop_index("ix_kb_items_frozen_by")
        batch.drop_index("ix_kb_items_derived_from")
        batch.drop_column("frozen_by")
        batch.drop_column("derived_from")
    for sql in views:
        op.execute(sql)
    with op.batch_alter_table("kb_versions") as batch:
        batch.drop_index("ix_kb_versions_batch_key")
        batch.drop_column("single_source")
        batch.drop_column("poison_flags")
        batch.drop_column("batch_key")
