"""三期 B17②：我的任务的退回事件 task_returns（主库）。

down_revision 接 main 链头 0050_runtime_config（决策记录 2026-11-23）。

Revision ID: 0043_task_returns
Revises: 0050_runtime_config
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_task_returns"
down_revision: str | None = "0050_runtime_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_returns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("ref_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_returns_ref_id", "task_returns", ["ref_id"])
    op.create_index("ix_task_returns_project_id", "task_returns", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_task_returns_project_id", table_name="task_returns")
    op.drop_index("ix_task_returns_ref_id", table_name="task_returns")
    op.drop_table("task_returns")
