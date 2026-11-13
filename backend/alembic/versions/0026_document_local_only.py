"""M4 ③：单个资料“仅现场使用”（project_documents.local_only 等）。

客户书面要求把某份资料改为仅现场使用（告知书附件一第 4 条）：项目经理标注后，平台删除该资料在对象存储里的
原件和解析结果，证据编号保留；同一文件不能再上传，Agent 读不到，交付件不能引用。标注不可撤销。

Revision ID: 0026_document_local_only
Revises: 0025_project_deletion
Create Date: 2026-11-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_document_local_only"
down_revision: str | None = "0025_project_deletion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("project_documents") as batch:
        batch.add_column(sa.Column("local_only", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("local_only_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("local_only_by", sa.String(64), nullable=False, server_default=""))
        batch.add_column(sa.Column("local_only_ref", sa.String(64), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("project_documents") as batch:
        batch.drop_column("local_only_ref")
        batch.drop_column("local_only_by")
        batch.drop_column("local_only_at")
        batch.drop_column("local_only")
