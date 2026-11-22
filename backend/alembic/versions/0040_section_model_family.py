"""三期 G2：section_versions 记生成模型和家族（Agent 写入记当次生成模型，顾问手改沿用上一版），质量评审据此排除同家族。

Revision ID: 0040_section_model_family
Revises: 0038_judgment_events
Create Date: 2026-11-22

0039 预分配给 F1（三期计划第七节），F1 先合并时把 down_revision 改成 0039。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_section_model_family"
down_revision: str | None = "0038_judgment_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("section_versions") as b:
        b.add_column(sa.Column("model", sa.String(128), nullable=False, server_default=""))
        b.add_column(sa.Column("model_family", sa.String(32), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("section_versions") as b:
        b.drop_column("model_family")
        b.drop_column("model")
