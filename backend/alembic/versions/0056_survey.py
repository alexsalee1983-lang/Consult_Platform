"""四期 E2①：调研助手题库、调研包、回传答案（survey_questions / survey_packs / survey_answers）。

Revision ID: 0056_survey
Revises: 0055_extraction_questions
Create Date: 2026-11-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_survey"
down_revision: str | None = "0055_extraction_questions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "survey_questions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("qid", sa.String(32), nullable=False),
        sa.Column("control", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("products", sa.JSON(), nullable=False),
        sa.Column("industries", sa.JSON(), nullable=False),
        sa.Column("variants", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("unclear_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_survey_questions_qid", "survey_questions", ["qid"], unique=True)
    op.create_index("ix_survey_questions_control", "survey_questions", ["control"])
    op.create_table(
        "survey_packs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("pack_hash", sa.String(64), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("qids", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("returned", sa.Integer(), server_default="0", nullable=False),
        sa.UniqueConstraint("project_id", "code", name="uq_survey_packs_code"),
    )
    op.create_index("ix_survey_packs_project_id", "survey_packs", ["project_id"])
    op.create_table(
        "survey_answers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("pack_id", sa.Integer(), sa.ForeignKey("survey_packs.id"), nullable=False),
        sa.Column("qid", sa.String(32), nullable=False),
        sa.Column("control", sa.String(64), nullable=False),
        sa.Column("respondent_role", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("value", sa.String(16), server_default="", nullable=False),
        sa.Column("text", sa.Text(), server_default="", nullable=False),
        sa.Column("fact_status", sa.String(16), server_default="", nullable=False),
        sa.Column("conflict", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("confirmed_role", sa.String(32), server_default="", nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for col in ("project_id", "pack_id", "qid"):
        op.create_index(f"ix_survey_answers_{col}", "survey_answers", [col])


def downgrade() -> None:
    for col in ("project_id", "pack_id", "qid"):
        op.drop_index(f"ix_survey_answers_{col}", table_name="survey_answers")
    op.drop_table("survey_answers")
    op.drop_index("ix_survey_packs_project_id", table_name="survey_packs")
    op.drop_table("survey_packs")
    op.drop_index("ix_survey_questions_control", table_name="survey_questions")
    op.drop_index("ix_survey_questions_qid", table_name="survey_questions")
    op.drop_table("survey_questions")
