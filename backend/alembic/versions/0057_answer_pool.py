"""四期 E3①：答案池（扩展 0056 survey 表，不另建平行表）。

survey_questions.key_control：关键控制项，总是人工确认（E1 题库可选字段 key_control）。
survey_answers.channel：来源渠道 prefill 资料预填 / self_assess 客户自评包 / onsite 现场题卡 / discussion 开放讨论；
survey_answers.source：可信度来源 evidence 现场证据 > document 客户资料 > oral 现场口述 > self 客户自评；
survey_answers.spot_check：自动确认的抽检状态（空 / pending / passed / failed）；fact_status 新增 auto（自动确认）。

Revision ID: 0057_answer_pool
Revises: 0056_survey
Create Date: 2026-11-25
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057_answer_pool"
down_revision: str | None = "0056_survey"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("survey_questions", sa.Column("key_control", sa.Boolean(), server_default=sa.false(),
                                                nullable=False))
    op.add_column("survey_answers", sa.Column("channel", sa.String(16), server_default="onsite", nullable=False))
    op.add_column("survey_answers", sa.Column("source", sa.String(16), server_default="oral", nullable=False))
    op.add_column("survey_answers", sa.Column("spot_check", sa.String(16), server_default="", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("survey_answers") as b:
        for col in ("spot_check", "source", "channel"):
            b.drop_column(col)
    with op.batch_alter_table("survey_questions") as b:
        b.drop_column("key_control")
