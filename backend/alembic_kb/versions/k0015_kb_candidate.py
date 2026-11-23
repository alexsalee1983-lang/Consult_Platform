"""三期 B24①：候选影子期，知识版本加 candidate_since（进入候选的时间）、candidate_result（晋级结果，默认空）。

B10、B11 自动发布通过的版本先落 status=candidate（status 是 String(16)，不用改列），只供评测和影子检索；
满 KB_CANDIDATE_DAYS 天且抽检没有判不通过的晋级为 published。只记时间和结果，不记正文。

合并时按知识区迁移编号顺序改接 down_revision（k0010～k0014 在其他未合并的 PR 里）。

Revision ID: k0015_kb_candidate
Revises: k0014_kb_sources
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0015_kb_candidate"
down_revision: str | None = "k0014_kb_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_versions") as batch:
        batch.add_column(sa.Column("candidate_since", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("candidate_result", sa.String(16), nullable=False, server_default=""))


def downgrade() -> None:
    # 降级前把还在候选的版本退回待审（旧代码不认 candidate，留着既不进交付也没人处理）
    # 候选的切片一并删掉（旧代码发布时会重新切片）
    op.execute("DELETE FROM kb_chunks WHERE version_id IN (SELECT id FROM kb_versions WHERE status = 'candidate')")
    op.execute("UPDATE kb_versions SET status = 'in_review' WHERE status = 'candidate'")
    with op.batch_alter_table("kb_versions") as batch:
        batch.drop_column("candidate_result")
        batch.drop_column("candidate_since")
