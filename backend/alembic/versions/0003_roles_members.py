"""M1：系统角色（roles、user_roles）与项目成员（project_members）。

角色清单随迁移写入，代码中的常量见 app/core/permissions.py。

Revision ID: 0003_roles_members
Revises: 0002_users
Create Date: 2026-11-08
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_roles_members"
down_revision: str | None = "0002_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = [
    ("consultant", "顾问"),
    ("project_lead", "项目负责人"),
    ("knowledge_expert", "知识专家"),
    ("knowledge_admin", "知识管理员"),
    ("sys_admin", "系统管理员"),
    ("security_auditor", "安全审计员"),
]


def upgrade() -> None:
    roles = op.create_table(
        "roles",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("code"),
    )
    op.bulk_insert(roles, [{"code": c, "name": n} for c, n in ROLES])

    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role_code", sa.String(length=32), nullable=False),
        sa.Column("granted_by", sa.String(length=64), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["role_code"], ["roles.code"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id", "role_code"),
    )

    op.create_table(
        "project_members",
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_role", sa.String(length=16), nullable=False),
        sa.Column("added_by", sa.String(length=64), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("project_id", "user_id"),
    )
    op.create_index("ix_project_members_user_id", "project_members", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_project_members_user_id", table_name="project_members")
    op.drop_table("project_members")
    op.drop_table("user_roles")
    op.drop_table("roles")
