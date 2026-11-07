"""审计哈希链：只追加、篡改可检出、并发写入不断链。对应 M1 验收“审计日志被篡改可检出”。"""
import importlib.util
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker

from app.cli import main as cli_main
from app.core import audit
from app.core.migrate import upgrade_to_head
from app.main import app
from app.models import AuditLog

from .conftest import create_user, logged_in_client


@pytest.fixture()
def chain_db(tmp_path: Path):
    """独立的库：升级到最新（含创世记录和触发器），写入 5 条记录。"""
    eng = create_engine(f"sqlite:///{tmp_path / 'chain.db'}")
    upgrade_to_head(eng)
    with Session(eng) as db:
        for i in range(5):
            audit.record(db, actor=f"user{i}", action="llm.chat", result="allowed", project_code="BANK-A", n=i)
    yield eng
    eng.dispose()


def _drop_triggers(eng):
    """模拟能绕过应用、直接改库的人（例如拿到数据库口令）。"""
    with eng.begin() as conn:
        conn.execute(text("DROP TRIGGER audit_logs_no_update"))
        conn.execute(text("DROP TRIGGER audit_logs_no_delete"))


def _ids(eng):
    with eng.connect() as conn:
        return [r[0] for r in conn.execute(text("SELECT id FROM audit_logs ORDER BY id"))]


def _verify(eng, expect_head=None):
    with Session(eng) as db:
        return audit.verify_chain(db, expect_head)


def test_fresh_chain_verifies(chain_db):
    r = _verify(chain_db)
    assert r.ok and r.checked == 6 and r.legacy == 0  # 创世记录 + 5 条
    assert r.head_hash and len(r.head_hash) == 64


def test_migration_and_app_use_the_same_hash():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0004_audit_chain.py"
    spec = importlib.util.spec_from_file_location("m0004", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    args = ("0" * 64, datetime(2026, 11, 8, 1, 2, 3, 456789, tzinfo=timezone.utc), "张三", "BANK-A",
            "llm.chat", "allowed", '{"k": "值"}')
    assert mod._chain_hash(*args) == audit.chain_hash(*args)


def test_database_rejects_update_and_delete(chain_db):
    with chain_db.begin() as conn, pytest.raises(DatabaseError, match="append-only"):
        conn.execute(text("UPDATE audit_logs SET actor = 'x' WHERE id = 2"))
    with chain_db.begin() as conn, pytest.raises(DatabaseError, match="append-only"):
        conn.execute(text("DELETE FROM audit_logs WHERE id = 2"))


def test_modified_record_detected(chain_db):
    _drop_triggers(chain_db)
    target = _ids(chain_db)[3]
    with chain_db.begin() as conn:
        conn.execute(text("UPDATE audit_logs SET result = 'blocked' WHERE id = :i"), {"i": target})
    r = _verify(chain_db)
    assert not r.ok and r.error_id == target and "内容" in r.error


def test_deleted_record_detected(chain_db):
    _drop_triggers(chain_db)
    ids = _ids(chain_db)
    with chain_db.begin() as conn:
        conn.execute(text("DELETE FROM audit_logs WHERE id = :i"), {"i": ids[2]})
    r = _verify(chain_db)
    assert not r.ok and r.error_id == ids[3]


def test_rehashed_forgery_still_breaks_next_link(chain_db):
    """改了内容并重算本条哈希，下一条的 prev_hash 仍然对不上。"""
    _drop_triggers(chain_db)
    ids = _ids(chain_db)
    with Session(chain_db) as db:
        row = db.get(AuditLog, ids[2])
        row.actor = "someone-else"
        row.hash = audit.chain_hash(row.prev_hash, row.at, row.actor, row.project_code, row.action, row.result,
                                    row.detail)
        db.commit()
    r = _verify(chain_db)
    assert not r.ok and r.error_id == ids[3]


def test_inserted_record_detected(chain_db):
    with chain_db.begin() as conn:  # 插入不受触发器限制，但不在链上
        conn.execute(text("INSERT INTO audit_logs (at, actor, project_code, action, result, detail) "
                          "VALUES ('2026-11-08 00:00:00.000000', 'x', '', 'llm.chat', 'allowed', '{}')"))
    r = _verify(chain_db)
    assert not r.ok and "不在链上" in r.error


def test_truncated_tail_detected_with_saved_head(chain_db):
    saved = _verify(chain_db).head_hash
    _drop_triggers(chain_db)
    with chain_db.begin() as conn:
        conn.execute(text("DELETE FROM audit_logs WHERE id = :i"), {"i": _ids(chain_db)[-1]})
    assert _verify(chain_db).ok                      # 只删末尾，链本身仍自洽（已知限制）
    r = _verify(chain_db, expect_head=saved)         # 用之前记下的链头就能发现
    assert not r.ok and "末尾" in r.error


def test_concurrent_writes_keep_single_chain(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'conc.db'}", connect_args={"check_same_thread": False})
    upgrade_to_head(eng)
    factory = sessionmaker(bind=eng)
    errors = []

    def worker(n):
        try:
            with factory() as db:
                for i in range(20):
                    audit.record(db, actor=f"t{n}", action="llm.chat", result="allowed", i=i)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    r = _verify(eng)
    assert r.ok and r.checked == 1 + 8 * 20
    with eng.connect() as conn:  # 没有两条记录接在同一个前驱上
        dup = conn.execute(text("SELECT prev_hash FROM audit_logs GROUP BY prev_hash HAVING COUNT(*) > 1")).all()
    assert dup == []
    eng.dispose()


# ---- 接口与命令行（主测试库） ----

@pytest.fixture(scope="module")
def auditor():
    c = logged_in_client(app, create_user(("security_auditor",)))
    yield c
    c.__exit__(None, None, None)


def test_verify_endpoint_and_is_itself_audited(auditor):
    r = auditor.get("/api/audit/verify")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["checked"] >= 1 and len(body["head_hash"]) == 64
    latest = auditor.get("/api/audit", params={"limit": 1}).json()[0]
    assert latest["action"] == "audit.verify" and latest["hash"]
    # 带上刚才的链头再校验，仍然通过
    assert auditor.get("/api/audit/verify", params={"expect_head": body["head_hash"]}).json()["ok"] is True


def test_audit_query_filters_and_paging(auditor):
    rows = auditor.get("/api/audit", params={"action": "auth.", "limit": 500}).json()
    assert rows and all(r["action"].startswith("auth.") for r in rows)
    blocked = auditor.get("/api/audit", params={"result": "blocked", "limit": 500}).json()
    assert all(r["result"] == "blocked" for r in blocked)
    page1 = auditor.get("/api/audit", params={"limit": 3}).json()
    page2 = auditor.get("/api/audit", params={"limit": 3, "before_id": page1[-1]["id"]}).json()
    assert page2 and max(r["id"] for r in page2) < min(r["id"] for r in page1)
    assert auditor.get("/api/audit", params={"result": "nope"}).status_code == 422


def test_audit_has_no_raw_text(auditor, make_user):
    member = logged_in_client(app, make_user(("consultant",), {"MFG-C": "member"}))
    try:
        member.post("/api/gateway/check", json={"text": "密码：Secret999"})
    finally:
        member.__exit__(None, None, None)
    rows = auditor.get("/api/audit", params={"action": "guard.check"}).json()
    assert rows and all("Secret999" not in r["detail"] for r in rows)


def test_cli_verify(capsys):
    assert cli_main(["verify-audit"]) == 0
    assert "完整" in capsys.readouterr().out


def test_main_db_chain_intact_after_all_activity():
    from app.core.db import SessionLocal

    with SessionLocal() as db:
        r = audit.verify_chain(db)
        assert r.ok, r.error
        assert db.scalar(select(AuditLog).where(AuditLog.action == audit.GENESIS_ACTION)) is not None
