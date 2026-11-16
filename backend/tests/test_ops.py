"""运维自动化（二期 C5 O5～O8）：每日自检进指标、删除逾期与备份时效指标、长期未登录自动停用。"""
import json
import uuid
from datetime import timedelta

from prometheus_client import generate_latest
from sqlalchemy import select

from app import worker
from app.core import audit, metrics
from app.core.config import get_settings
from app.core.db import SessionLocal, utcnow
from app.main import app
from app.models import AuditLog, Job, Project, ProjectDeletion, User
from app.services import jobs, ops

from .conftest import create_user, logged_in_client


def _value(name: str) -> float:
    return metrics.REGISTRY.get_sample_value(name) or 0.0


def _age(username: str, *, login_days: int | None = None, created_days: int | None = None) -> None:
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.username == username))
        if login_days is not None:
            u.last_login_at = utcnow() - timedelta(days=login_days)
        if created_days is not None:
            u.created_at = utcnow() - timedelta(days=created_days)
        db.commit()


def _active(username: str) -> bool:
    with SessionLocal() as db:
        return db.scalar(select(User.is_active).where(User.username == username))


def test_inactive_accounts_disabled_admin_kept_and_reenable_resets():
    days = get_settings().inactive_account_days
    stale = create_user(("consultant",))[0]
    never = create_user(("consultant",))[0]
    fresh = create_user(("consultant",))[0]
    old_admin = create_user(("sys_admin",))[0]
    _age(stale, login_days=days + 1)
    _age(never, created_days=days + 1)                 # 从没登录过：按建号时间算
    _age(fresh, login_days=1)
    _age(old_admin, login_days=days + 1)
    names = [stale, never, fresh, old_admin]
    with SessionLocal() as db:
        assert sorted(ops.disable_inactive(db, usernames=names)) == sorted([stale, never])
        assert ops.disable_inactive(db, usernames=names) == []          # 可重复执行
        log = db.scalar(select(AuditLog).where(AuditLog.action == "user.disable", AuditLog.actor == "platform")
                        .order_by(AuditLog.id.desc()))
        assert json.loads(log.detail)["trigger"] == ops.RULE_INACTIVE
    assert not _active(stale) and not _active(never) and _active(fresh) and _active(old_admin)
    admin = logged_in_client(app, create_user(("sys_admin",)))
    try:
        users = {u["username"]: u for u in admin.get("/api/admin/users").json()}
        assert users[old_admin]["inactive"] is True                     # 系统管理员只标出
        assert admin.post(f"/api/admin/users/{stale}/enable").status_code == 200
        users = {u["username"]: u for u in admin.get("/api/admin/users").json()}
        assert users[stale]["inactive"] is False
    finally:
        admin.__exit__(None, None, None)
    with SessionLocal() as db:
        assert ops.disable_inactive(db, usernames=[stale]) == []        # 启用后重新计时
        assert ops.disable_inactive(db, now=utcnow() + timedelta(days=days + 1), usernames=[stale]) == [stale]


def test_disable_off_when_days_zero(monkeypatch):
    u = create_user(("consultant",))[0]
    _age(u, login_days=10_000)
    monkeypatch.setattr(get_settings(), "inactive_account_days", 0)
    with SessionLocal() as db:
        assert ops.disable_inactive(db, usernames=[u]) == []


def test_daily_selfcheck_metric(monkeypatch):
    monkeypatch.setattr(ops, "disable_inactive", lambda db: [])      # 不动其他用例的账号
    worker._daily()
    worker._daily()                                                  # 已有排队中的不重复排
    with SessionLocal() as db:
        queued = db.scalars(select(Job).where(Job.kind == "system.selfcheck", Job.status == jobs.QUEUED,
                                              Job.created_by == "platform")).all()
        assert len(queued) == 1
        while jobs.run_once(db, "w-test", kinds=["system.selfcheck"]):
            pass
    assert _value("cp_selfcheck_ok") == 1 and _value("cp_selfcheck_timestamp_seconds") > 0
    with SessionLocal() as db:
        jobs.enqueue(db, "system.selfcheck", {"fail": True, "retry": False}, created_by="tester", max_attempts=1)
        while jobs.run_once(db, "w-test", kinds=["system.selfcheck"]):
            pass
    assert _value("cp_selfcheck_ok") == 0


def test_deletion_and_backup_state_metrics():
    metrics.register_state_collector()
    code = "OPS-" + uuid.uuid4().hex[:6].upper()
    today = utcnow().date()
    with SessionLocal() as db:
        before_open, before_overdue = ops.deletion_counts(db)
        p = Project(code=code, service="SOC 运营体系设计", industry="银行")
        db.add(p)
        db.commit()
        db.add_all([ProjectDeletion(project_id=p.id, kind="early", status="pending", requested_by="t",
                                    due_by=today - timedelta(days=1)),
                    ProjectDeletion(project_id=p.id, kind="early", status="done", requested_by="t",
                                    due_by=today - timedelta(days=3), completed_at=utcnow() - timedelta(days=4))])
        db.commit()
        assert ops.deletion_counts(db) == (before_open + 1, before_overdue + 1)
        month = [m for m in ops.monthly_deletions(db) if m["month"] == utcnow().strftime("%Y-%m")][0]
        assert month["overdue"] >= 1 and month["on_time"] >= 1
        audit.record(db, actor="cli", action="backup.create", result="allowed", stamp="20261115T020000Z",
                     head_id=1, bytes=10)
        last = ops.last_backup(db)
    text = generate_latest(metrics.REGISTRY).decode()
    assert "cp_deletions_overdue" in text and code not in text
    assert _value("cp_deletions_overdue") == before_overdue + 1
    assert abs(_value("cp_backup_last_success_timestamp_seconds") - last.timestamp()) < 1
    with SessionLocal() as db:                                     # 不留给其他用例
        for d in db.scalars(select(ProjectDeletion).where(ProjectDeletion.project_id == p.id)):
            d.status = "cancelled"
        db.commit()
