"""M2：知识区数据表（条目、版本、切片、审核、上传、去标识报告）。字段依据公共规范第 7 节，见 app/models/kb.py。

PostgreSQL 上给切片加中文全文检索生成列 tsv（配置 zh，迁移 0009 创建）和 GIN 索引；SQLite 跳过。
kb_items.current_version_id 与 kb_versions 互相引用，外键在两张表都建好后再加。

Revision ID: 0011_kb
Revises: 0010_jobs
Create Date: 2026-11-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_kb"
down_revision: str | None = "0010_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('kb_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('doc_id', sa.String(length=40), nullable=False),
    sa.Column('layer', sa.String(length=2), nullable=False),
    sa.Column('domain', sa.String(length=3), nullable=False),
    sa.Column('product', sa.String(length=8), nullable=False),
    sa.Column('doc_type', sa.String(length=3), nullable=False),
    sa.Column('stage', sa.String(length=8), nullable=False),
    sa.Column('capabilities', sa.JSON(), nullable=False),
    sa.Column('industries', sa.JSON(), nullable=False),
    sa.Column('classification', sa.String(length=16), nullable=False),
    sa.Column('visible_roles', sa.JSON(), nullable=False),
    sa.Column('owner', sa.String(length=64), nullable=False),
    sa.Column('current_version_id', sa.Integer(), nullable=True),
    sa.Column('retired', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('kb_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_kb_items_doc_id'), ['doc_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_kb_items_doc_type'), ['doc_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_kb_items_domain'), ['domain'], unique=False)
        batch_op.create_index(batch_op.f('ix_kb_items_layer'), ['layer'], unique=False)
        batch_op.create_index(batch_op.f('ix_kb_items_product'), ['product'], unique=False)
        batch_op.create_index(batch_op.f('ix_kb_items_stage'), ['stage'], unique=False)

    op.create_table('kb_uploads',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('object_key', sa.String(length=512), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('content_type', sa.String(length=128), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('guard_categories', sa.JSON(), nullable=False),
    sa.Column('error', sa.String(length=200), nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=True),
    sa.Column('uploaded_by', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('object_key')
    )
    with op.batch_alter_table('kb_uploads', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_kb_uploads_sha256'), ['sha256'], unique=False)
        batch_op.create_index(batch_op.f('ix_kb_uploads_status'), ['status'], unique=False)

    op.create_table('kb_deid_reports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('upload_id', sa.Integer(), nullable=True),
    sa.Column('replacements', sa.JSON(), nullable=False),
    sa.Column('residual_findings', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('reviewed_by', sa.String(length=64), nullable=False),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['upload_id'], ['kb_uploads.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('kb_deid_reports', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_kb_deid_reports_upload_id'), ['upload_id'], unique=False)

    op.create_table('kb_versions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('item_id', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('summary', sa.String(length=1000), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('applicability', sa.String(length=500), nullable=False),
    sa.Column('change_note', sa.String(length=1000), nullable=False),
    sa.Column('source_type', sa.String(length=16), nullable=False),
    sa.Column('source_detail', sa.String(length=300), nullable=False),
    sa.Column('ref_document', sa.String(length=200), nullable=False),
    sa.Column('ref_version', sa.String(length=64), nullable=False),
    sa.Column('ref_status', sa.String(length=16), nullable=False),
    sa.Column('deid_status', sa.String(length=16), nullable=False),
    sa.Column('deid_report_id', sa.Integer(), nullable=True),
    sa.Column('upload_id', sa.Integer(), nullable=True),
    sa.Column('effective_date', sa.Date(), nullable=True),
    sa.Column('review_date', sa.Date(), nullable=True),
    sa.Column('created_by', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('submitted_by', sa.String(length=64), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['deid_report_id'], ['kb_deid_reports.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['kb_items.id'], ),
    sa.ForeignKeyConstraint(['upload_id'], ['kb_uploads.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('item_id', 'version', name='uq_kb_versions_item_version')
    )
    with op.batch_alter_table('kb_versions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_kb_versions_item_id'), ['item_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_kb_versions_review_date'), ['review_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_kb_versions_status'), ['status'], unique=False)
    with op.batch_alter_table('kb_items', schema=None) as batch_op:
        batch_op.create_foreign_key('fk_kb_items_current_version', 'kb_versions', ['current_version_id'], ['id'])

    op.create_table('kb_chunks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('version_id', sa.Integer(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('heading', sa.String(length=300), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['version_id'], ['kb_versions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('version_id', 'seq', name='uq_kb_chunks_version_seq')
    )
    with op.batch_alter_table('kb_chunks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_kb_chunks_version_id'), ['version_id'], unique=False)

    op.create_table('kb_reviews',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('version_id', sa.Integer(), nullable=False),
    sa.Column('reviewer', sa.String(length=64), nullable=False),
    sa.Column('review_role', sa.String(length=16), nullable=False),
    sa.Column('decision', sa.String(length=16), nullable=False),
    sa.Column('comment', sa.String(length=1000), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['version_id'], ['kb_versions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('version_id', 'reviewer', name='uq_kb_reviews_version_reviewer')
    )
    with op.batch_alter_table('kb_reviews', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_kb_reviews_version_id'), ['version_id'], unique=False)

    if op.get_bind().dialect.name == "postgresql":
        # 中文全文检索：标题路径 + 正文，按 zh 配置分词，写入时自动生成
        op.execute("ALTER TABLE kb_chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS "
                   "(to_tsvector('zh'::regconfig, coalesce(heading, '') || ' ' || text)) STORED")
        op.execute("CREATE INDEX ix_kb_chunks_tsv ON kb_chunks USING gin (tsv)")


def downgrade() -> None:
    with op.batch_alter_table('kb_items', schema=None) as batch_op:
        batch_op.drop_constraint('fk_kb_items_current_version', type_='foreignkey')
    with op.batch_alter_table('kb_reviews', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_kb_reviews_version_id'))

    op.drop_table('kb_reviews')
    with op.batch_alter_table('kb_chunks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_kb_chunks_version_id'))

    op.drop_table('kb_chunks')
    with op.batch_alter_table('kb_versions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_kb_versions_status'))
        batch_op.drop_index(batch_op.f('ix_kb_versions_review_date'))
        batch_op.drop_index(batch_op.f('ix_kb_versions_item_id'))

    op.drop_table('kb_versions')
    with op.batch_alter_table('kb_deid_reports', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_kb_deid_reports_upload_id'))

    op.drop_table('kb_deid_reports')
    with op.batch_alter_table('kb_uploads', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_kb_uploads_status'))
        batch_op.drop_index(batch_op.f('ix_kb_uploads_sha256'))

    op.drop_table('kb_uploads')
    with op.batch_alter_table('kb_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_kb_items_stage'))
        batch_op.drop_index(batch_op.f('ix_kb_items_product'))
        batch_op.drop_index(batch_op.f('ix_kb_items_layer'))
        batch_op.drop_index(batch_op.f('ix_kb_items_domain'))
        batch_op.drop_index(batch_op.f('ix_kb_items_doc_type'))
        batch_op.drop_index(batch_op.f('ix_kb_items_doc_id'))

    op.drop_table('kb_items')
