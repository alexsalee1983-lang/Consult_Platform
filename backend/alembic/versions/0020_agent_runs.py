"""M3 ④：交付链运行记录（agent_runs、agent_run_steps）。

平台自写流程控制（决策记录 2026-11-10，不用 LangGraph）：标准档交付链 3→4→7 的每一步由后台任务推进，
运行状态、发过的确认卡、每步的 Agent / 提示词版本 / 模型 / 用量 / 产出引用记在这两张表里。
不新增工具、不改 Agent × 工具授权。

Revision ID: 0020_agent_runs
Revises: 0019_confirm_cards
Create Date: 2026-11-12
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_agent_runs"
down_revision: str | None = "0019_confirm_cards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("waiting_cards", sa.JSON(), nullable=False),
        sa.Column("card_ids", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(length=200), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_runs_project_id", "agent_runs", ["project_id"])
    op.create_table(
        "agent_run_steps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("step", sa.String(length=16), nullable=False),
        sa.Column("agent_code", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("output", sa.Text(), nullable=False),
        sa.Column("refs", sa.JSON(), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_run_steps_run_id", "agent_run_steps", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_run_steps_run_id", table_name="agent_run_steps")
    op.drop_table("agent_run_steps")
    op.drop_index("ix_agent_runs_project_id", table_name="agent_runs")
    op.drop_table("agent_runs")
