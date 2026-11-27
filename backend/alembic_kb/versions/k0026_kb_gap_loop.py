"""四期 B41：知识缺口闭环补齐——kb_gaps 加缺什么、取正文网址、失败次数、推荐记录；新表 kb_alarms（入库报警卡）。

kb_gaps 新列：
- need：缺哪类资料（法规 / 标准 / 样例 / 条目），就绪度缺格、引用缺失、生命周期类缺口写；
- source_url / source_via：要去取的正文网址与来路（agent 来源推荐 / listing 栏目页解析），缺口键是编号时下发包用它；
- attempts / unavailable_at：获取失败次数，满 3 次标“暂无可用来源”（plan=unavailable），每月自动再试；
- rec_total / rec_at：来源推荐 Agent 给过几次推荐（含被程序校验丢弃的），供推荐命中率。
plan 新增取值 unavailable，字段长度不变。

Revision ID: k0026_kb_gap_loop
Revises: k0022_kb_gap_manual
Create Date: 2026-11-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0026_kb_gap_loop"
down_revision: str | None = "k0022_kb_gap_manual"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_gaps") as batch:
        batch.add_column(sa.Column("need", sa.String(8), nullable=False, server_default=""))
        batch.add_column(sa.Column("source_url", sa.String(500), nullable=False, server_default=""))
        batch.add_column(sa.Column("source_via", sa.String(8), nullable=False, server_default=""))
        batch.add_column(sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("unavailable_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("rec_total", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("rec_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "kb_alarms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="open"),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("batches", sa.JSON(), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_kb_alarms_status", "kb_alarms", ["status"])


def downgrade() -> None:
    op.drop_index("ix_kb_alarms_status", table_name="kb_alarms")
    op.drop_table("kb_alarms")
    with op.batch_alter_table("kb_gaps") as batch:
        for col in ("rec_at", "rec_total", "unavailable_at", "attempts", "source_via", "source_url", "need"):
            batch.drop_column(col)
