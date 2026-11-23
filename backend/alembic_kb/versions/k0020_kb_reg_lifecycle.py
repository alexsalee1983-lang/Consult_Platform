"""三期 B26：法规生命周期——依据版本记废止日 repeal_date、替代的旧文件 ref_replaces、部分修订的文件 ref_amends。

施行日沿用 effective_date；被替代沿用 ref_replaced_by。每日后台任务按日期切换现行状态（app/kb/reg_lifecycle.py）。
编号按三期计划第七节顺序排在 k0019（#293）之后；本分支基于 main，先接 k0009，合并时由后合并的一方改接到链尾。

Revision ID: k0020_kb_reg_lifecycle
Revises: k0018_kb_practice_decisions
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0020_kb_reg_lifecycle"
down_revision: str | None = "k0018_kb_practice_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_versions") as batch:
        batch.add_column(sa.Column("repeal_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("ref_replaces", sa.String(200), nullable=False, server_default=""))
        batch.add_column(sa.Column("ref_amends", sa.String(200), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("kb_versions") as batch:
        batch.drop_column("ref_amends")
        batch.drop_column("ref_replaces")
        batch.drop_column("repeal_date")
