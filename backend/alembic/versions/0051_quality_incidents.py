"""四期 F3：AI 质量事件台账 quality_incidents、quality_incident_events（只追加，库级触发器拒绝改、删）。

Revision ID: 0051_quality_incidents
Revises: 0040_section_model_family（main 链头；down_revision 合并时接当时链头，决策记录 2026-11-23）
Create Date: 2026-11-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051_quality_incidents"
down_revision: str | None = "0040_section_model_family"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("quality_incidents", "quality_incident_events")


def upgrade() -> None:
    op.create_table(
        "quality_incidents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_code", sa.String(32), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("ref_kind", sa.String(16), nullable=False),
        sa.Column("ref_id", sa.String(64), nullable=False),
        sa.Column("description_len", sa.Integer(), nullable=False),
        sa.Column("description_sha", sa.String(16), nullable=False),
        sa.Column("reported_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_quality_incidents_project_code", "quality_incidents", ["project_code"])
    op.create_index("ix_quality_incidents_created_at", "quality_incidents", ["created_at"])
    op.create_table(
        "quality_incident_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("incident_id", sa.Integer(), sa.ForeignKey("quality_incidents.id"), nullable=False),
        sa.Column("step", sa.String(16), nullable=False),
        sa.Column("level", sa.String(4), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("root_cause", sa.String(16), nullable=False),
        sa.Column("note_len", sa.Integer(), nullable=False),
        sa.Column("note_sha", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_quality_incident_events_incident_id", "quality_incident_events", ["incident_id"])
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("""
            CREATE FUNCTION quality_incidents_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
            END $$;
        """)
        for t in TABLES:
            op.execute(f"CREATE TRIGGER {t}_no_update_delete BEFORE UPDATE OR DELETE ON {t} "
                       "FOR EACH ROW EXECUTE FUNCTION quality_incidents_append_only()")
            op.execute(f"CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t} "
                       "FOR EACH STATEMENT EXECUTE FUNCTION quality_incidents_append_only()")
    elif dialect == "sqlite":
        for t in TABLES:
            op.execute(f"CREATE TRIGGER {t}_no_update BEFORE UPDATE ON {t} "
                       f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END")
            op.execute(f"CREATE TRIGGER {t}_no_delete BEFORE DELETE ON {t} "
                       f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END")


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    for t in TABLES:
        if dialect == "postgresql":
            op.execute(f"DROP TRIGGER IF EXISTS {t}_no_update_delete ON {t}")
            op.execute(f"DROP TRIGGER IF EXISTS {t}_no_truncate ON {t}")
        elif dialect == "sqlite":
            op.execute(f"DROP TRIGGER IF EXISTS {t}_no_update")
            op.execute(f"DROP TRIGGER IF EXISTS {t}_no_delete")
    if dialect == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS quality_incidents_append_only()")
    op.drop_index("ix_quality_incident_events_incident_id", table_name="quality_incident_events")
    op.drop_table("quality_incident_events")
    op.drop_index("ix_quality_incidents_created_at", table_name="quality_incidents")
    op.drop_index("ix_quality_incidents_project_code", table_name="quality_incidents")
    op.drop_table("quality_incidents")
