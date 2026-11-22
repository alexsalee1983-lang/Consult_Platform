"""三期 F1：发现表 findings（字段对齐 OSCAL 评估结果与 POA&M，加责任方）。

Revision ID: 0039_findings
Revises: 0038_judgment_events
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_findings"
down_revision: str | None = "0038_judgment_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _s(name: str, n: int) -> sa.Column:
    return sa.Column(name, sa.String(n), nullable=False, server_default="")


def upgrade() -> None:
    op.create_table(
        "findings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("fid", sa.String(8), nullable=False),
        _s("control_code", 64), _s("domain", 40), _s("item", 200), _s("current", 500),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        _s("basis_clause", 120), _s("basis_version", 32), _s("conclusion", 16), _s("agent_conclusion", 16),
        sa.Column("current_score", sa.Float(), nullable=True),
        sa.Column("target_score", sa.Float(), nullable=True),
        _s("impact", 4), _s("likelihood", 4), _s("risk", 500), _s("priority", 8), _s("owner_dept", 64),
        _s("period", 32), _s("acceptance", 500), _s("recommendation", 500), _s("responsibility", 16),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("source", sa.String(16), nullable=False, server_default="agent"),
        _s("agent_code", 32),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("agent_runs.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "fid", name="uq_findings_project_fid"),
    )
    op.create_index("ix_findings_project_id", "findings", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_findings_project_id", table_name="findings")
    op.drop_table("findings")
