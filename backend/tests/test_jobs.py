"""后台任务队列：入队、领取、重试与退避、租期回收、并发领取不重复、管理接口、健康检查。"""
import json
import threading
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.core.db import SessionLocal, utcnow
from app.main import app
from app.models import AuditLog, Job
from app.services import jobs

from .conftest import create_user, logged_in_client, on_postgres

CALLS: list[int] = []


@jobs.handler("test.ok")
def _ok(payload, ctx):
    CALLS.append(ctx.job_id)


@jobs.handler("test.boom")
def _boom(payload, ctx):
    raise ValueError("某某城商行 核心系统 口令 Abc@12345")   # 异常原文不得进 last_error


@jobs.handler("test.fail")
def _fail(payload, ctx):
    raise jobs.JobError("解析失败：文件损坏", retry=payload.get("retry", True))


def _enqueue(kind, payload=None, **kw):
    with SessionLocal() as db:
        return jobs.enqueue(db, kind, payload, created_by="tester", **kw)


def _run(kinds, worker="w1", lease=300):
    with SessionLocal() as db:
        return jobs.run_once(db, worker, lease, kinds=kinds)


def _job(job_id) -> Job:
    with SessionLocal() as db:
        return db.get(Job, job_id)


def _make_due(job_id):
    with SessionLocal() as db:
        db.execute(update(Job).where(Job.id == job_id).values(run_after=utcnow() - timedelta(seconds=1)))
        db.commit()


def test_enqueue_rejects_unknown_kind_and_large_payload():
    with pytest.raises(ValueError, match="未注册"):
        _enqueue("test.nope")
    with pytest.raises(ValueError, match="过大"):
        _enqueue("test.ok", {"text": "x" * 5000})


def test_run_ok_marks_done_and_audits():
    jid = _enqueue("test.ok", {"upload_id": 7}, project_code="BANK-A")
    assert _run(["test.ok"]) == jobs.DONE
    j = _job(jid)
    assert j.status == "done" and j.attempts == 1 and jid in CALLS and j.locked_until is None
    with SessionLocal() as db:
        a = db.scalars(select(AuditLog).where(AuditLog.action == "job.done").order_by(AuditLog.id.desc())).first()
    assert json.loads(a.detail)["job_id"] == jid and a.project_code == "BANK-A"
    assert _run(["test.ok"]) is None                 # 没有到期任务


def test_retry_with_backoff_then_failed_and_audited():
    jid = _enqueue("test.fail", max_attempts=2)
    assert _run(["test.fail"]) == jobs.QUEUED
    j = _job(jid)
    assert j.attempts == 1 and "文件损坏" in j.last_error
    assert _run(["test.fail"]) is None               # 退避期内不领取
    _make_due(jid)
    assert _run(["test.fail"]) == jobs.FAILED
    with SessionLocal() as db:
        a = db.scalars(select(AuditLog).where(AuditLog.action == "job.failed").order_by(AuditLog.id.desc())).first()
    assert json.loads(a.detail)["job_id"] == jid and a.result == "error"


def test_no_retry_fails_immediately():
    jid = _enqueue("test.fail", {"retry": False}, max_attempts=5)
    assert _run(["test.fail"]) == jobs.FAILED and _job(jid).attempts == 1


def test_unexpected_exception_records_type_only():
    jid = _enqueue("test.boom", max_attempts=1)
    assert _run(["test.boom"]) == jobs.FAILED
    j = _job(jid)
    assert j.last_error == "ValueError"
    with SessionLocal() as db:
        details = [a.detail for a in db.scalars(select(AuditLog).where(AuditLog.action == "job.failed"))]
    assert all("某某城商行" not in d and "Abc@12345" not in d for d in details)


def test_expired_lease_is_reclaimed_by_another_worker():
    jid = _enqueue("test.ok")
    with SessionLocal() as db:
        job = jobs.claim(db, "crashed-worker", lease_seconds=300, kinds=["test.ok"])
        assert job.id == jid
    with SessionLocal() as db:
        assert jobs.claim(db, "w2", kinds=["test.ok"]) is None     # 租期内别人领不到
        db.execute(update(Job).where(Job.id == jid).values(locked_until=utcnow() - timedelta(seconds=1)))
        db.commit()
    assert _run(["test.ok"], worker="w2") == jobs.DONE
    j = _job(jid)
    assert j.attempts == 2 and j.worker == "w2"


def test_backoff_grows_and_caps():
    assert [jobs.backoff_seconds(n) for n in (1, 2, 3)] == [30, 60, 120]
    assert jobs.backoff_seconds(20) == jobs.BACKOFF_MAX_SECONDS


def test_admin_retry_requeues_failed_job():
    jid = _enqueue("test.fail", {"retry": False})
    _run(["test.fail"])
    c = logged_in_client(app, create_user(("sys_admin",)))
    try:
        assert c.post(f"/api/admin/jobs/{jid}/retry").json()["status"] == "queued"
        assert c.post(f"/api/admin/jobs/{jid}/retry").status_code == 404     # 已不是失败状态
        body = c.get("/api/admin/jobs").json()
        row = next(r for r in body["recent"] if r["id"] == jid)
        assert "payload" not in row and "project_code" not in row            # 管理员看不到业务数据
        r = c.post("/api/admin/jobs/selfcheck")
        assert r.status_code == 202
    finally:
        c.__exit__(None, None, None)
    sid = r.json()["job_id"]
    while _run(["system.selfcheck"]):   # 其他测试可能也排了自检任务，全部执行完
        pass
    assert _job(sid).status == "done"
    _run(["test.fail"])   # 清理重新排队的任务


def test_health_reports_worker(monkeypatch):
    from fastapi.testclient import TestClient

    from app import worker

    monkeypatch.setattr(worker, "online_workers", lambda: 1)
    with TestClient(app) as c:
        assert c.get("/api/health").json()["checks"]["worker"] == "ok"
    monkeypatch.setattr(worker, "online_workers", lambda: 0)
    with TestClient(app) as c:
        assert c.get("/api/health").json()["checks"]["worker"].startswith("down")


def test_heartbeat_touches_alive_file(tmp_path, monkeypatch):
    from app import worker

    monkeypatch.setattr(worker, "ALIVE_FILE", tmp_path / "alive")
    worker.heartbeat(None, "w-test")
    assert (tmp_path / "alive").exists()


@on_postgres
def test_concurrent_workers_never_claim_same_job():
    ids = {_enqueue("test.ok") for _ in range(30)}
    claimed: list[int] = []
    lock = threading.Lock()

    def loop(name):
        while True:
            with SessionLocal() as db:
                job = jobs.claim(db, name, kinds=["test.ok"])
                if job is None:
                    return
                with lock:
                    claimed.append(job.id)
                jobs.run(db, job)

    threads = [threading.Thread(target=loop, args=(f"w{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(ids)            # 每条恰好领取一次
