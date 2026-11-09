"""M2：知识切片的向量列。kb_chunks 增加 embedding_model；PostgreSQL 上增加 embedding vector(512) 和 HNSW 索引（余弦距离）。

维度 512 对应 BAAI/bge-small-zh-v1.5（决策记录 2026-11-08）。换成维度不同的模型（如公司平台的 embedding）时，
新建迁移改列的维度，并执行 python -m app.cli kb-reembed 重新向量化。

Revision ID: 0014_kb_embedding
Revises: 0013_kb_deid
Create Date: 2026-11-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_kb_embedding"
down_revision: str | None = "0013_kb_deid"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DIM = 512


def upgrade() -> None:
    with op.batch_alter_table("kb_chunks") as batch:
        batch.add_column(sa.Column("embedding_model", sa.String(length=64), nullable=False, server_default=""))
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"ALTER TABLE kb_chunks ADD COLUMN embedding vector({DIM})")
        op.execute("CREATE INDEX ix_kb_chunks_embedding ON kb_chunks USING hnsw (embedding vector_cosine_ops)")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_kb_chunks_embedding")
        op.execute("ALTER TABLE kb_chunks DROP COLUMN IF EXISTS embedding")
    with op.batch_alter_table("kb_chunks") as batch:
        batch.drop_column("embedding_model")
