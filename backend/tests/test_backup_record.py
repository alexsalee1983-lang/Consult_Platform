"""备份 / 恢复脚本记审计：只记时间戳、链头编号和字节数；时间戳格式不对拒绝。"""
import json

from sqlalchemy import select

from app.cli import main as cli_main
from app.core.db import SessionLocal
from app.models import AuditLog


def test_backup_record():
    assert cli_main(["backup-record", "--op", "create", "--stamp", "20270110T020000Z", "--head-id", "42",
                     "--bytes", "1024"]) == 0
    with SessionLocal() as db:
        row = db.scalars(select(AuditLog).where(AuditLog.action == "backup.create")
                         .order_by(AuditLog.id.desc())).first()
    assert row.actor == "cli" and json.loads(row.detail) == {"stamp": "20270110T020000Z", "head_id": 42, "bytes": 1024}
    assert cli_main(["backup-record", "--op", "restore", "--stamp", "x; rm -rf /"]) == 1
