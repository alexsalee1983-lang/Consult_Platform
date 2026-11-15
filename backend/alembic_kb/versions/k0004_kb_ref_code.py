"""二期 B7：依据元数据加文号或标准号 ref_code、替代文件 ref_replaced_by（默认空）。

依据原文切条（app/kb/sources.py）按下载清单填写；ref_status 新增取值 superseded（已被替代），
质量评审按已废止处理（二期 B9）。已有版本一律为空，不影响现有数据。

接在 B3 的 k0003_kb_mappings 之后（与 e2 约定：#113 先合并）。

Revision ID: k0004_kb_ref_code
Revises: k0003_kb_mappings
Create Date: 2026-11-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0004_kb_ref_code"
down_revision: str | None = "k0003_kb_mappings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kb_versions") as batch:
        batch.add_column(sa.Column("ref_code", sa.String(100), nullable=False, server_default=""))
        batch.add_column(sa.Column("ref_replaced_by", sa.String(200), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("kb_versions") as batch:
        batch.drop_column("ref_replaced_by")
        batch.drop_column("ref_code")
