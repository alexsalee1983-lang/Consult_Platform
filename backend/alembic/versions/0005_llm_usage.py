"""M1：大模型用量表 llm_usage（按人、项目、模型计量，不记内容）。

Revision ID: 0005_llm_usage
Revises: 0004_audit_chain
Create Date: 2026-11-08
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_llm_usage"
down_revision: str | None = "0004_audit_chain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("project_code", sa.String(length=32), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("demo", sa.Boolean(), nullable=False),
        sa.Column("stream", sa.Boolean(), nullable=False),
        sa.Column("pseudonymized", sa.Boolean(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in ("at", "username", "project_code", "model"):
        op.create_index(f"ix_llm_usage_{col}", "llm_usage", [col])


def downgrade() -> None:
    for col in ("at", "username", "project_code", "model"):
        op.drop_index(f"ix_llm_usage_{col}", table_name="llm_usage")
    op.drop_table("llm_usage")
