"""M2：后台任务表 jobs（解析、向量化、到期删除等由 worker 执行）。

Revision ID: 0010_jobs
Revises: 0009_zh_search
Create Date: 2026-11-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_jobs"
down_revision: str | None = "0009_zh_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker", sa.String(length=64), nullable=False),
        sa.Column("last_error", sa.String(length=200), nullable=False),
        sa.Column("project_code", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_kind", "jobs", ["kind"])
    op.create_index("ix_jobs_project_code", "jobs", ["project_code"])
    op.create_index("ix_jobs_status_run_after", "jobs", ["status", "run_after"])


def downgrade() -> None:
    op.drop_index("ix_jobs_status_run_after", table_name="jobs")
    op.drop_index("ix_jobs_project_code", table_name="jobs")
    op.drop_index("ix_jobs_kind", table_name="jobs")
    op.drop_table("jobs")
