"""三期 G2③：知识条目版本记起草模型和家族 model、model_family（默认空）。

Agent 起草的条目导入时记起草模型，知识审核据此换家族（异构评审）；专家修订没写模型的沿用上一版。
三期计划第七节迁移表把知识条目写在主链 0040，但 kb_versions 在知识区库，按分库约定改用知识区链 k0016。
down_revision 暂接 k0009；B15（k0010～k0012）、B19（k0013）、B16（k0014）、B24（k0015）先合并时改接最新一个。

Revision ID: k0016_kb_version_model
Revises: k0009_kb_upload_project
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0016_kb_version_model"
down_revision: str | None = "k0009_kb_upload_project"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_versions") as batch:
        batch.add_column(sa.Column("model", sa.String(100), nullable=False, server_default=""))
        batch.add_column(sa.Column("model_family", sa.String(32), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("kb_versions") as batch:
        batch.drop_column("model_family")
        batch.drop_column("model")
