"""M1：审计哈希链。

1. audit_logs 增加 prev_hash、hash 两列。已有记录（M0 和登录功能上线初期）保持 hash 为空，作为“链前记录”。
2. 写入一条创世记录（action=audit.genesis，prev_hash 为 64 个 0），新链从它开始。
3. 数据库触发器禁止 UPDATE / DELETE（PostgreSQL 另禁止 TRUNCATE），审计表只能追加。

哈希算法与 app/core/audit.py 的 chain_hash 必须一致。这里单独写一份（迁移脚本不随应用代码变化），
tests/test_audit_chain.py 校验两者对创世记录算出的哈希相同。

Revision ID: 0004_audit_chain
Revises: 0003_roles_members
Create Date: 2026-11-08
"""
import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "0004_audit_chain"
down_revision: str | None = "0003_roles_members"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GENESIS_PREV = "0" * 64


def _chain_hash(prev_hash, at, actor, project_code, action, result, detail):
    at_text = at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    payload = json.dumps([at_text, actor, project_code, action, result, detail], ensure_ascii=False,
                         separators=(",", ":"))
    return hashlib.sha256((prev_hash + "\n" + payload).encode("utf-8")).hexdigest()


def upgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("audit_logs") as batch:
        batch.add_column(sa.Column("prev_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("hash", sa.String(length=64), nullable=True))
        batch.create_index("ix_audit_logs_hash", ["hash"], unique=True)

    legacy = bind.execute(sa.text("SELECT COUNT(*) FROM audit_logs")).scalar_one()
    at = datetime.now(timezone.utc)
    detail = json.dumps({"legacy_records": legacy, "note": "审计哈希链起点；此前的记录为链前记录"}, ensure_ascii=False)
    row = {"at": at, "actor": "system", "project_code": "", "action": "audit.genesis", "result": "allowed",
           "detail": detail, "prev_hash": GENESIS_PREV}
    row["hash"] = _chain_hash(GENESIS_PREV, at, "system", "", "audit.genesis", "allowed", detail)
    audit_logs = sa.table(
        "audit_logs",
        sa.column("at", sa.DateTime(timezone=True)), sa.column("actor", sa.String), sa.column("project_code", sa.String),
        sa.column("action", sa.String), sa.column("result", sa.String), sa.column("detail", sa.Text),
        sa.column("prev_hash", sa.String), sa.column("hash", sa.String),
    )
    op.bulk_insert(audit_logs, [row])

    if bind.dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION audit_logs_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              RAISE EXCEPTION 'audit_logs is append-only';
            END $$;
        """)
        op.execute("CREATE TRIGGER audit_logs_no_update_delete BEFORE UPDATE OR DELETE ON audit_logs "
                   "FOR EACH ROW EXECUTE FUNCTION audit_logs_append_only()")
        op.execute("CREATE TRIGGER audit_logs_no_truncate BEFORE TRUNCATE ON audit_logs "
                   "FOR EACH STATEMENT EXECUTE FUNCTION audit_logs_append_only()")
    elif bind.dialect.name == "sqlite":
        op.execute("CREATE TRIGGER audit_logs_no_update BEFORE UPDATE ON audit_logs "
                   "BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END")
        op.execute("CREATE TRIGGER audit_logs_no_delete BEFORE DELETE ON audit_logs "
                   "BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_logs_no_truncate ON audit_logs")
        op.execute("DROP TRIGGER IF EXISTS audit_logs_no_update_delete ON audit_logs")
        op.execute("DROP FUNCTION IF EXISTS audit_logs_append_only()")
    elif bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS audit_logs_no_update")
        op.execute("DROP TRIGGER IF EXISTS audit_logs_no_delete")
    with op.batch_alter_table("audit_logs") as batch:
        batch.drop_index("ix_audit_logs_hash")
        batch.drop_column("hash")
        batch.drop_column("prev_hash")
