"""后台任务队列：数据库存任务，Redis 只负责唤醒 worker。

- enqueue()：写一条 queued 任务并提交，再往 Redis 推一个唤醒信号（Redis 不可用时 worker 也会定时轮询，不丢任务）。
- claim()：worker 领取一条到期任务。PostgreSQL 上用 FOR UPDATE SKIP LOCKED，多个 worker 同时领取也不会重复；
  领取后设租期（locked_until），worker 崩溃时租期过后任务会被别的 worker 重新领取。
- 失败按指数退避重试（30 秒、1 分钟、2 分钟……最长 1 小时），超过 max_attempts 次标记 failed 并写审计。
- 处理器必须可重复执行（同一任务可能因超时被执行两次），用 @handler("种类") 注册。
- payload 只放编号、对象键等引用（最多 4KB），不放客户原文；last_error 只记错误类型和 JobError 的说明，不记异常原文。
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..core import audit, metrics
from ..core.db import utcnow
from ..models import Job

WAKE_KEY = "jobs:wake"
MAX_PAYLOAD = 4096
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600
DEFAULT_LEASE_SECONDS = 300

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"

_handlers: dict[str, Callable[[dict, JobContext], None]] = {}
_claim_lock = threading.Lock()   # SQLite（单元测试）没有 SKIP LOCKED，进程内串行领取


class JobError(Exception):
    """处理器主动抛出的错误：message 必须是不含客户原文的说明，会写进 last_error。retry=False 表示不再重试。"""

    def __init__(self, message: str, retry: bool = True):
        super().__init__(message)
        self.retry = retry


@dataclass
class JobContext:
    job_id: int
    kind: str
    attempt: int
    project_code: str
    created_by: str


def handler(kind: str):
    def deco(fn):
        if kind in _handlers:
            raise ValueError(f"任务种类重复注册：{kind}")
        _handlers[kind] = fn
        return fn
    return deco


def registered() -> list[str]:
    return sorted(_handlers)


def _as_aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=utcnow().tzinfo)


def _wake() -> None:
    try:
        import redis

        from ..core.config import get_settings

        redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1).lpush(WAKE_KEY, 1)
    except Exception:  # noqa: BLE001  唤醒失败不影响任务，worker 会定时轮询
        pass


def enqueue(db: Session, kind: str, payload: dict | None = None, *, created_by: str, project_code: str = "",
            max_attempts: int = 5, delay_seconds: int = 0) -> int:
    if kind not in _handlers:
        raise ValueError(f"未注册的任务种类：{kind}")
    text_payload = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
    if len(text_payload.encode()) > MAX_PAYLOAD:
        raise ValueError("任务参数过大：只放编号、对象键等引用，不放文件内容或原文")
    job = Job(kind=kind, payload=text_payload, status=QUEUED, attempts=0, max_attempts=max_attempts,
              run_after=utcnow() + timedelta(seconds=delay_seconds), project_code=project_code,
              created_by=created_by[:64])
    db.add(job)
    db.commit()
    _wake()
    return job.id


def claim(db: Session, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS,
          kinds: Sequence[str] | None = None) -> Job | None:
    """领取一条到期任务（含租期已过的 running 任务），返回时已提交为 running。kinds 限定只领这些种类。"""
    now = utcnow()
    due = or_(and_(Job.status == QUEUED, Job.run_after <= now),
              and_(Job.status == RUNNING, Job.locked_until < now))
    q = select(Job).where(due).order_by(Job.run_after, Job.id).limit(1)
    if kinds:
        q = q.where(Job.kind.in_(list(kinds)))
    postgres = db.get_bind().dialect.name == "postgresql"
    if postgres:
        q = q.with_for_update(skip_locked=True)
    with _claim_lock if not postgres else _nullctx():
        job = db.scalars(q).first()
        if job is None:
            db.rollback()
            return None
        job.status = RUNNING
        job.attempts += 1
        job.worker = worker_id[:64]
        job.started_at = now
        job.locked_until = now + timedelta(seconds=lease_seconds)
        db.commit()
    return job


class _nullctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def backoff_seconds(attempt: int) -> int:
    return min(BACKOFF_BASE_SECONDS * 2 ** max(attempt - 1, 0), BACKOFF_MAX_SECONDS)


def _finish(db: Session, job: Job, status: str, error: str = "") -> None:
    job.status = status
    job.locked_until = None
    job.finished_at = utcnow()
    job.last_error = error[:200]
    db.commit()


def run(db: Session, job: Job) -> str:
    """执行一条已领取的任务，返回最终状态（done / queued 等待重试 / failed）。"""
    kind, start = job.kind, time.perf_counter()
    status = _run(db, job)
    metrics.JOBS.labels(kind, status).inc()
    metrics.JOB_LATENCY.labels(kind).observe(time.perf_counter() - start)
    return status


def _run(db: Session, job: Job) -> str:
    fn = _handlers.get(job.kind)
    ctx = JobContext(job_id=job.id, kind=job.kind, attempt=job.attempts, project_code=job.project_code,
                     created_by=job.created_by)
    try:
        if fn is None:
            raise JobError(f"没有处理器：{job.kind}", retry=False)
        fn(json.loads(job.payload or "{}"), ctx)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        retry = getattr(e, "retry", True)
        error = f"{type(e).__name__}: {e}" if isinstance(e, JobError) else type(e).__name__
        if retry and job.attempts < job.max_attempts:
            job.status = QUEUED
            job.locked_until = None
            job.last_error = error[:200]
            job.run_after = utcnow() + timedelta(seconds=backoff_seconds(job.attempts))
            db.commit()
            return QUEUED
        _finish(db, job, FAILED, error)
        audit.record(db, actor="worker", action="job.failed", result="error", project_code=job.project_code,
                     job_id=job.id, kind=job.kind, attempts=job.attempts, error=error[:200])
        return FAILED
    _finish(db, job, DONE)
    audit.record(db, actor="worker", action="job.done", result="allowed", project_code=job.project_code,
                 job_id=job.id, kind=job.kind, attempts=job.attempts)
    return DONE


def run_once(db: Session, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS,
             kinds: Sequence[str] | None = None) -> str | None:
    job = claim(db, worker_id, lease_seconds, kinds)
    return run(db, job) if job else None


def retry(db: Session, job_id: int) -> Job | None:
    """把失败的任务重新排队（管理员操作，调用方写审计）。"""
    job = db.get(Job, job_id)
    if job is None or job.status != FAILED:
        return None
    job.status, job.attempts, job.run_after, job.last_error = QUEUED, 0, utcnow(), ""
    job.finished_at = None
    db.commit()
    _wake()
    return job


def summary(db: Session) -> dict:
    rows = db.execute(select(Job.kind, Job.status, func.count()).group_by(Job.kind, Job.status)).all()
    out: dict[str, dict[str, int]] = {}
    for kind, status, n in rows:
        out.setdefault(kind, {})[status] = n
    return out


# ---- 内置任务 ----

@handler("system.selfcheck")
def _selfcheck(payload: dict, ctx: JobContext) -> None:
    """worker 自检：能连数据库；启用对象存储时在项目区写入、读回、删除一个测试对象。"""
    from sqlalchemy import text

    from ..core.db import SessionLocal
    from . import storage

    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    if storage.enabled():
        key = f"SELFCHECK/job-{ctx.job_id}"
        body = f"selfcheck {ctx.job_id}".encode()
        storage.put("project", key, body, "text/plain")
        try:
            if storage.get("project", key) != body:
                raise JobError("对象存储读回内容不一致")
        finally:
            storage.delete("project", key)
    if payload.get("fail"):          # 测试用：模拟失败
        raise JobError("按要求模拟失败", retry=bool(payload.get("retry", True)))
