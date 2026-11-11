"""M3：项目区资料与证据编号（project_documents、evidences），新工具“查证据”project.evidence。

授权：project.evidence 是项目区只读工具，授权与“读项目区”一列完全相同（8 个交付侧 Agent），
负责人 2026-11-10 同意 S3 拆分时确认；知识侧 Agent 不授权，硬规则也会拒绝。

Revision ID: 0017_project_evidence
Revises: 0016_kb_import_key
Create Date: 2026-11-10
"""
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "0017_project_evidence"
down_revision: str | None = "0016_kb_import_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GRANTED_TO = ["consultant-lead", "delivery-pm", "research-interview", "compliance-basis", "solution-designer",
              "policy-drafter", "report-writer", "qa-reviewer"]


def upgrade() -> None:
    op.create_table(
        "project_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=200), nullable=False),
        sa.Column("parsed_key", sa.String(length=512), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("section_count", sa.Integer(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("uploaded_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
        sa.UniqueConstraint("project_id", "sha256", name="uq_project_documents_sha"),
    )
    op.create_index("ix_project_documents_project_id", "project_documents", ["project_id"])
    op.create_table(
        "evidences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("interviewee_role", sa.String(length=100), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["project_documents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "code", name="uq_evidences_code"),
    )
    op.create_index("ix_evidences_project_id", "evidences", ["project_id"])

    tools = sa.table("tools", sa.column("code", sa.String), sa.column("name", sa.String),
                     sa.column("zone", sa.String), sa.column("access", sa.String))
    op.bulk_insert(tools, [{"code": "project.evidence", "name": "查证据", "zone": "project", "access": "read"}])
    grants = sa.table("agent_tool_grants", sa.column("agent_code", sa.String), sa.column("tool_code", sa.String),
                      sa.column("granted_by", sa.String), sa.column("granted_at", sa.DateTime(timezone=True)))
    now = datetime.now(timezone.utc)
    op.bulk_insert(grants, [{"agent_code": a, "tool_code": "project.evidence", "granted_by": "migration:0017",
                             "granted_at": now} for a in GRANTED_TO])


def downgrade() -> None:
    op.execute("DELETE FROM agent_tool_grants WHERE tool_code = 'project.evidence'")
    op.execute("DELETE FROM tools WHERE code = 'project.evidence'")
    op.drop_index("ix_evidences_project_id", table_name="evidences")
    op.drop_table("evidences")
    op.drop_index("ix_project_documents_project_id", table_name="project_documents")
    op.drop_table("project_documents")
