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


# ③ 下发包导出 / 回传批次导入（CLI kb-acquire-dispatch、kb-acquire-import）
import hashlib  # noqa: E402
import json  # noqa: E402

from app import cli  # noqa: E402

GP = "https://gp.example.gov.cn/"


def _planned_direct(db) -> str:
    _active_source(db, GP)
    url = GP + "law/1.html"
    gap_pool.record(db, [Signal(url, "external", "EV-D1", impact=5)])
    gap_pool.record(db, [Signal("GB/T 99999", "usage", "EV-D2")])          # 不是网址 → 来源确认，不下发
    gap_pool.run_week(db, TODAY)
    return url


def test_dispatch_package_only_direct_whitelisted(tmp_path):
    with SessionLocal() as db:
        url = _planned_direct(db)
        pkg = gap_pool.dispatch_package(db)
        assert [t["url"] for t in pkg["tasks"]] == [url]
        assert set(pkg) == {"format", "tasks"}
        a = db.scalar(select(AuditLog).where(AuditLog.action == "kb.gap.dispatch").order_by(AuditLog.id.desc()))
        assert a is not None and json.loads(a.detail)["tasks"] == 1
    out = tmp_path / "pkg.json"
    assert cli.main(["kb-acquire-dispatch", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["tasks"][0]["url"] == url


def _batch(tmp_path, url: str, *, tamper: bool = False, extra: bool = False):
    b = tmp_path / "batch"
    b.mkdir()
    (b / "a.html").write_bytes(b"<html>public</html>")
    digest = hashlib.sha256(b"<html>public</html>").hexdigest()
    if tamper:
        (b / "a.html").write_bytes(b"changed")
    if extra:
        (b / "x.bin").write_bytes(b"?")
    (b / "manifest.json").write_text(json.dumps({"files": [{"file": "a.html", "sha256": digest, "url": url}]}),
                                     encoding="utf-8")
    return b


def test_import_rejects_bad_batch_and_records_categories_only(tmp_path):
    with SessionLocal() as db:
        url = _planned_direct(db)
    b = _batch(tmp_path, url, tamper=True, extra=True)
    assert cli.main(["kb-acquire-import", str(b)]) == 2
    with SessionLocal() as db:
        a = db.scalar(select(AuditLog).where(AuditLog.action == "kb.gap.return").order_by(AuditLog.id.desc()))
        assert a.result == "blocked" and json.loads(a.detail)["problems"] == {"extra": 1, "sha256": 1}
        assert "a.html" not in a.detail
        assert db.scalar(select(KbGap).where(KbGap.gap_key == url)).status == "open"


def test_import_rejects_offlist_url(tmp_path):
    with SessionLocal() as db:
        _planned_direct(db)
    b = _batch(tmp_path, "https://evil.example.com/a.html")
    assert cli.main(["kb-acquire-import", str(b)]) == 2


def test_import_ok_closes_collected_gaps(tmp_path):
    with SessionLocal() as db:
        url = _planned_direct(db)
    b = _batch(tmp_path, url)
    assert cli.main(["kb-acquire-import", str(b)]) == 0                  # 不带 --manifest：只核对、关缺口
    with SessionLocal() as db:
        g = db.scalar(select(KbGap).where(KbGap.gap_key == url))
        assert g.status == "closed" and g.close_reason == "collected"
        a = db.scalar(select(AuditLog).where(AuditLog.action == "kb.gap.return").order_by(AuditLog.id.desc()))
        assert a.result == "allowed" and json.loads(a.detail)["files"] == 1 and json.loads(a.detail)["closed"] == 1


def test_publish_manifest_built_from_registry_and_unmatched_listed(tmp_path):
    import csv
    import io

    with SessionLocal() as db:
        url = _planned_direct(db)
        src = db.scalar(select(KbSource).where(KbSource.url == GP))
        src.publisher = "虚拟发布机关"
        db.commit()
        b = _batch(tmp_path, url)
        text, unmatched = gap_pool.publish_manifest(db, b)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert unmatched == []
    assert rows == [{"文件名": "a.html", "名称": "测试来源", "发布机关": "虚拟发布机关", "原网址": url,
                     "现行状态": "现行", "获取方式": "已下载",
                     "sha256": hashlib.sha256(b"<html>public</html>").hexdigest()}]


def test_publish_manifest_skips_files_without_registered_source(tmp_path):
    with SessionLocal() as db:
        _planned_direct(db)
        src = db.scalar(select(KbSource).where(KbSource.url == GP))
        src.status = "candidate"                                    # 候选来源不算登记，不能自动入库
        db.commit()
        text, unmatched = gap_pool.publish_manifest(db, _batch(tmp_path, GP + "law/1.html"))
    assert unmatched == ["a.html"] and text.count("\n") == 1          # 只有表头


def test_import_auto_manifest_needs_as_and_hands_csv_to_publish(tmp_path, monkeypatch):
    with SessionLocal() as db:
        url = _planned_direct(db)
    b = _batch(tmp_path, url)
    assert cli.main(["kb-acquire-import", str(b), "--auto-manifest"]) == 1
    seen: dict = {}

    def fake_publish(args):
        seen["csv"] = open(args.manifest, encoding="utf-8").read()
        seen["dir"] = args.dir
        return 0

    monkeypatch.setattr(cli, "kb_source_publish", fake_publish)
    assert cli.main(["kb-acquire-import", str(b), "--auto-manifest", "--as", "kbadmin"]) == 0
    assert "测试来源" in seen["csv"] and seen["dir"] == str(b)
