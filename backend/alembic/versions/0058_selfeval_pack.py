"""四期 E3②：客户自评包（从调研包按受访角色派生；口令与答卷原文不存）。

survey_selfeval_packs：包号 S-xx、版本、受访角色、包哈希、题号、截止日、状态（issued / sent / imported / superseded）、
发出时间、提醒阶段（0 / 1 前一天 / 2 当天 / 3 逾期升级项目经理）、导入时间与导入的答案编号（撤销用）。

Revision ID: 0058_selfeval_pack
Revises: 0057_answer_pool
Create Date: 2026-11-26
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0058_selfeval_pack"
down_revision: str | None = "0057_answer_pool"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "survey_selfeval_packs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("pack_id", sa.Integer(), sa.ForeignKey("survey_packs.id"), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("respondent_role", sa.String(32), nullable=False),
        sa.Column("pack_hash", sa.String(64), nullable=False),
        sa.Column("qids", sa.JSON(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), server_default="issued", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remind_stage", sa.Integer(), server_default="0", nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answer_ids", sa.JSON(), nullable=False),
        sa.Column("unclear_qids", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "code", name="uq_survey_selfeval_code"),
    )
    op.create_index("ix_survey_selfeval_packs_project_id", "survey_selfeval_packs", ["project_id"])
    op.create_index("ix_survey_selfeval_packs_pack_id", "survey_selfeval_packs", ["pack_id"])


def downgrade() -> None:
    op.drop_index("ix_survey_selfeval_packs_pack_id", table_name="survey_selfeval_packs")
    op.drop_index("ix_survey_selfeval_packs_project_id", table_name="survey_selfeval_packs")
    op.drop_table("survey_selfeval_packs")
