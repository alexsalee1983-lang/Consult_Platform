"""三期 B25②：缺口池表 kb_gaps（知识区 k0019）、每周计划任务、关闭率与平均关闭时长。"""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.core.db import SessionLocal
from app.kb import gap_pool
from app.kb.acquisition import Signal
from app.models import AuditLog, KbGap, KbSource
from app.services import jobs

TODAY = date(2026, 12, 7)


@pytest.fixture(autouse=True)
def clean():
    with SessionLocal() as db:
        db.execute(delete(KbGap))
        db.execute(delete(KbSource).where(KbSource.url.like("https://gp.example.gov.cn/%")))
        db.commit()
    yield


def _active_source(db, url: str) -> None:
    now = datetime.now(timezone.utc)
    db.add(KbSource(code=f"SR-G{abs(hash(url)) % 9999:04d}", name="测试来源", url=url, host="gp.example.gov.cn",
                    license="公开发布", authority_kind="法律", authority_level=1, status="active",
                    created_by="t", created_at=now, updated_at=now))
    db.commit()


def test_record_merges_and_dedupes_events():
    with SessionLocal() as db:
        gap_pool.record(db, [Signal("GB/T 22239", "external", "EV-1", impact=3, authority=2),
                             Signal("GB/T 22239", "usage", "EV-2", impact=8, due=date(2026, 12, 20))])
        gap_pool.record(db, [Signal("GB/T 22239", "external", "EV-1")])       # 同一事件不重复计
        g = db.scalar(select(KbGap).where(KbGap.gap_key == "GB/T 22239"))
        assert g.status == "open" and g.events == ["EV-1", "EV-2"]
        assert g.kinds == {"external": 1, "usage": 1} and g.impact == 8 and g.authority == 2
        assert g.due == date(2026, 12, 20)


def test_record_rejects_raw_text_in_event_ref():
    with SessionLocal() as db, pytest.raises(ValueError):
        gap_pool.record(db, [Signal("K1", "usage", "客户说他们的口令是 abc")])


def test_run_week_plans_by_whitelist_threshold_and_cap():
    url = "https://gp.example.gov.cn/law-1"
    with SessionLocal() as db:
        _active_source(db, "https://gp.example.gov.cn/")
        gap_pool.record(db, [Signal(url, "external", "EV-10", impact=5, authority=1),
                             Signal("GB/T 99999", "external", "EV-11", impact=1),
                             Signal("GB/T 88888", "usage", "EV-12", impact=1),
                             Signal("GB/T 77777", "business", "EV-13", impact=0)])
        out = gap_pool.run_week(db, TODAY, thresholds={"usage": 2}, cap=2)
        assert out == {"direct": 1, "confirm": 1, "deferred": 1, "waiting": 1}
        plans = {g.gap_key: g.plan for g in db.scalars(select(KbGap))}
        assert plans[url] == "direct" and plans["GB/T 88888"] == "waiting"
        assert sorted(plans.values()) == ["confirm", "deferred", "direct", "waiting"]
        assert all(g.planned_at is not None and g.score >= 0 for g in db.scalars(select(KbGap)))
        detail = db.scalars(select(AuditLog.detail).where(AuditLog.action == "kb.gap.week")).all()
        assert detail and all("GB/T" not in d and "gp.example" not in d for d in detail)  # 审计只记数量


def test_close_reopen_and_stats():
    with SessionLocal() as db:
        gap_pool.record(db, [Signal("K-A", "usage", "E1"), Signal("K-B", "usage", "E2"), Signal("K-C", "usage", "E3")])
        t0 = datetime(2026, 12, 1, tzinfo=timezone.utc)
        for k in ("K-A", "K-B", "K-C"):
            db.scalar(select(KbGap).where(KbGap.gap_key == k)).opened_at = t0
        db.commit()
        gap_pool.close(db, "K-A", "published", now=t0 + timedelta(days=2))
        gap_pool.close(db, "K-B", "dropped", now=t0 + timedelta(days=4))
        with pytest.raises(ValueError):
            gap_pool.close(db, "K-C", "随便写")
        with pytest.raises(LookupError):
            gap_pool.close(db, "K-A", "published")                       # 已关闭的不能再关
        s = gap_pool.stats(db)
        assert s["total"] == 3 and s["closed"] == 2 and s["open"] == 1
        assert s["close_rate"] == pytest.approx(2 / 3, abs=1e-3) and s["avg_close_days"] == pytest.approx(3.0)
        assert s["by_reason"] == {"published": 1, "dropped": 1}
        gap_pool.record(db, [Signal("K-A", "external", "E9")])           # 关闭后又来新信号：重新打开
        g = db.scalar(select(KbGap).where(KbGap.gap_key == "K-A"))
        assert g.status == "open" and g.closed_at is None and g.reopened == 1 and g.close_reason == ""


def test_weekly_job_registered_and_runs():
    assert gap_pool.JOB_KIND in jobs.registered()
    with SessionLocal() as db:
        gap_pool.record(db, [Signal("K-J", "business", "E1")])
    gap_pool._job({"today": TODAY.isoformat()}, None)
    with SessionLocal() as db:
        assert db.scalar(select(KbGap).where(KbGap.gap_key == "K-J")).plan == "confirm"
