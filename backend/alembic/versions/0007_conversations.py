"""M1：Agent 对话（conversations、messages），llm_usage 增加 agent_code；修正旧 0006 登记的方案设计 Agent 代号。

Revision ID: 0007_conversations
Revises: 0006_agents_tools
Create Date: 2026-11-08
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_conversations"
down_revision: str | None = "0006_agents_tools"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rename_solution_designer() -> None:
    """0006 早期版本把方案设计 Agent 登记为 solution-design，后改为与 md 一致的 solution-designer。
    已经跑过旧 0006 的库（开发机、测试机）在这里修正：登记新代号、迁移授权、删除旧代号。新库不受影响。"""
    bind = op.get_bind()
    old = bind.execute(sa.text("SELECT 1 FROM agents WHERE code = 'solution-design'")).first()
    new = bind.execute(sa.text("SELECT 1 FROM agents WHERE code = 'solution-designer'")).first()
    if not old or new:
        return
    bind.execute(sa.text("INSERT INTO agents (code, name, side, phase, enabled) "
                         "SELECT 'solution-designer', name, side, phase, enabled FROM agents "
                         "WHERE code = 'solution-design'"))
    bind.execute(sa.text("INSERT INTO agent_tool_grants (agent_code, tool_code, granted_by, granted_at) "
                         "SELECT 'solution-designer', tool_code, granted_by, granted_at FROM agent_tool_grants "
                         "WHERE agent_code = 'solution-design'"))
    bind.execute(sa.text("DELETE FROM agent_tool_grants WHERE agent_code = 'solution-design'"))
    bind.execute(sa.text("DELETE FROM agents WHERE code = 'solution-design'"))


def upgrade() -> None:
    _rename_solution_designer()
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("agent_code", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_code"], ["agents.code"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversations_project_id", "conversations", ["project_id"])
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_index("ix_conversations_updated_at", "conversations", ["updated_at"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=16), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])

    with op.batch_alter_table("llm_usage") as batch:
        batch.add_column(sa.Column("agent_code", sa.String(length=32), nullable=False, server_default=""))
        batch.create_index("ix_llm_usage_agent_code", ["agent_code"])


def downgrade() -> None:
    with op.batch_alter_table("llm_usage") as batch:
        batch.drop_index("ix_llm_usage_agent_code")
        batch.drop_column("agent_code")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversations_updated_at", table_name="conversations")
    op.drop_index("ix_conversations_user_id", table_name="conversations")
    op.drop_index("ix_conversations_project_id", table_name="conversations")
    op.drop_table("conversations")
