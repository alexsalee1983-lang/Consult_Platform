"""M5 ③：知识区与项目区分库的前置改动——去掉 kb_uploads.job_id 指向 jobs 的外键。

分库后 kb_ 开头的表在知识区数据库，jobs 留在主库，跨库不能建外键；job_id 只作记录，平台代码不靠外键。
此后知识区表结构只经 alembic_kb（知识区迁移链，版本表 alembic_version_kb）修改，本迁移链不再动 kb_ 表。

Revision ID: 0030_kb_split
Revises: 0029_password_changed_at
Create Date: 2026-11-13
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0030_kb_split"
down_revision: str | None = "0029_password_changed_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLite 上的外键没有名字，批处理模式按命名约定给它一个名字才能删
_NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def _has_table(name: str) -> bool:
    from sqlalchemy import inspect

    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("kb_uploads"):   # 已分库、主库里的知识区表已移走
        return
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE kb_uploads DROP CONSTRAINT IF EXISTS kb_uploads_job_id_fkey")
        return
    with op.batch_alter_table("kb_uploads", naming_convention=_NAMING) as batch:
        batch.drop_constraint("fk_kb_uploads_job_id_jobs", type_="foreignkey")


def downgrade() -> None:
    if not _has_table("kb_uploads"):
        return
    with op.batch_alter_table("kb_uploads") as batch:
        batch.create_foreign_key("kb_uploads_job_id_fkey", "jobs", ["job_id"], ["id"])
