"""M2：对话消息记录本轮附给 Agent 的知识库条目（messages.kb_refs），供界面显示“参考知识”。

Revision ID: 0015_message_references
Revises: 0014_kb_embedding
Create Date: 2026-11-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_message_references"
down_revision: str | None = "0014_kb_embedding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("kb_refs", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("kb_refs")
