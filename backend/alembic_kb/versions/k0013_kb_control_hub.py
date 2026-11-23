"""三期 B19：控制项中枢表结构（内容可先空，种子数据来自 knowledge/controls，#237、#245）。

- kb_frameworks：标准 / 框架（代号 + 版本），换版时旧版标 superseded，列出受影响映射；
- kb_controls：公司控制项（编号全局唯一、只增不改，按目录版本记），适用性规则与组织定义参数存 JSON；
- kb_control_checkpoints：控制项下的检查点（参照 SP 800-53A 评估目标），题库、评分锚定、发现初判挂这里；
- kb_control_mappings：控制项 ↔ 框架条款，关系类型（等价 / 子集 / 超集 / 交集，NIST IR 8477、SCF STRM）、强度 1～10、理由，带映射版本。
只存条款号与简短理由，不存标准原文。迁移号按三期开发计划第七节表；down 暂接 k0009（B15 链合并后由后合并的一方改接）。

Revision ID: k0013_kb_control_hub
Revises: k0012_kb_doc_id
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0013_kb_control_hub"
down_revision: str | None = "k0012_kb_doc_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kb_frameworks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="current"),
        sa.Column("superseded_by_id", sa.Integer(), sa.ForeignKey("kb_frameworks.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code", "version", name="uq_kb_frameworks_code_version"),
    )
    op.create_table(
        "kb_controls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("catalog_version", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("product", sa.String(16), nullable=False),
        sa.Column("domain", sa.String(60), nullable=False),
        sa.Column("industries", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("applicability", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("params", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("status", sa.String(12), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code", "catalog_version", name="uq_kb_controls_code_version"),
    )
    op.create_index("ix_kb_controls_code", "kb_controls", ["code"])
    op.create_index("ix_kb_controls_product", "kb_controls", ["product"])
    op.create_table(
        "kb_control_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("control_id", sa.Integer(), sa.ForeignKey("kb_controls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(48), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("objective", sa.String(500), nullable=False),
        sa.Column("methods", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("status", sa.String(12), nullable=False, server_default="draft"),
        sa.UniqueConstraint("control_id", "code", name="uq_kb_control_checkpoints_control_code"),
    )
    op.create_index("ix_kb_control_checkpoints_control_id", "kb_control_checkpoints", ["control_id"])
    op.create_index("ix_kb_control_checkpoints_code", "kb_control_checkpoints", ["code"])
    op.create_table(
        "kb_control_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("control_id", sa.Integer(), sa.ForeignKey("kb_controls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("framework_id", sa.Integer(), sa.ForeignKey("kb_frameworks.id"), nullable=False),
        sa.Column("clause", sa.String(40), nullable=False),
        sa.Column("relation", sa.String(8), nullable=False),
        sa.Column("strength", sa.Integer(), nullable=False),
        sa.Column("rationale", sa.String(300), nullable=False, server_default=""),
        sa.Column("mapping_version", sa.String(16), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("control_id", "framework_id", "clause", "mapping_version",
                            name="uq_kb_control_mappings_key"),
    )
    op.create_index("ix_kb_control_mappings_control_id", "kb_control_mappings", ["control_id"])
    op.create_index("ix_kb_control_mappings_framework_clause", "kb_control_mappings", ["framework_id", "clause"])


def downgrade() -> None:
    op.drop_index("ix_kb_control_mappings_framework_clause", table_name="kb_control_mappings")
    op.drop_index("ix_kb_control_mappings_control_id", table_name="kb_control_mappings")
    op.drop_table("kb_control_mappings")
    op.drop_index("ix_kb_control_checkpoints_code", table_name="kb_control_checkpoints")
    op.drop_index("ix_kb_control_checkpoints_control_id", table_name="kb_control_checkpoints")
    op.drop_table("kb_control_checkpoints")
    op.drop_index("ix_kb_controls_product", table_name="kb_controls")
    op.drop_index("ix_kb_controls_code", table_name="kb_controls")
    op.drop_table("kb_controls")
    op.drop_table("kb_frameworks")
