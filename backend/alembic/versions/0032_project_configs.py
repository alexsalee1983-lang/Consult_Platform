"""二期 A3：立项快照 project_configs（docs/接口约定.md 第四节）。

一期已有项目不在这里补行：后端启动时平台按 products/soc.yaml、无模块补建（app/project/configs.py），
迁移不依赖配置文件能否读出。

Revision ID: 0032_project_configs
Revises: 0030_kb_split（#98 的 0031 合并后改为 0031_document_text_sha）
Create Date: 2026-11-14
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_project_configs"
down_revision: str | None = "0030_kb_split"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False, unique=True),
        sa.Column("product", sa.String(8), nullable=False),
        sa.Column("modules", sa.JSON(), nullable=False),
        sa.Column("product_version", sa.String(16), nullable=False),
        sa.Column("module_versions", sa.JSON(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("project_configs")
