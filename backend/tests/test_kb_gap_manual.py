"""四期 B25：缺口池“需人工下载”。

采集机取不到的网址（回传 manifest 的 failed）把对应缺口转为需人工下载并记原因类别；每周计划不再把它排回直接采集；
知识专家、知识管理员在知识审批页看到清单、上传原件，原件经对象存储进后台任务，按回传批次同一口径核对
（哈希、正式来源白名单）后走 kb-source-publish，通过才关缺口（collected）。审计不记文件名。
"""
import hashlib
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.core.db import SessionLocal, utcnow
from app.core.permissions import CONSULTANT, KNOWLEDGE_ADMIN, KNOWLEDGE_EXPERT
from app.kb import acquisition as acq
from app.kb import gap_manual, gap_pool
from app.kb.acquisition import Signal
from app.main import app
from app.models import AuditLog, KbGap, KbSource
from app.services import storage

from .conftest import create_user, logged_in_client

TODAY = date(2026, 12, 7)
HOST = "https://mn.example.gov.sg"
URL1, URL2 = f"{HOST}/act/pdpa", f"{HOST}/act/csa"


@pytest.fixture(scope="module")
def people():
    users = {"expert": create_user((KNOWLEDGE_EXPERT,)), "admin": create_user((KNOWLEDGE_ADMIN,)),
             "consultant": create_user((CONSULTANT,))}
    made = {k: logged_in_client(app, u) for k, u in users.items()}
    made["_names"] = {k: u[0] for k, u in users.items()}
    yield made
    for k, c in made.items():
        if k != "_names":
            c.__exit__(None, None, None)


@pytest.fixture(autouse=True)
def clean():
    def wipe() -> None:
        with SessionLocal() as db:
            db.execute(delete(KbGap))
            db.execute(delete(KbSource).where(KbSource.url.like(f"{HOST}%")))
            db.commit()
    wipe()
    with SessionLocal() as db:
        now = utcnow()
        db.add(KbSource(code="SR-M0001", name="示范国法规在线", url=f"{HOST}/act/", host="mn.example.gov.sg",
                        license="政府公开文本", authority_kind="法律", authority_level=1, status="active",
                        publisher="示范国总检察署", created_by="t", created_at=now, updated_at=now))
        db.commit()
    yield
    wipe()


@pytest.fixture
def fake_storage(monkeypatch):
    objs: dict[str, bytes] = {}
    monkeypatch.setattr(storage, "enabled", lambda: True)
    monkeypatch.setattr(storage, "put", lambda area, key, data, content_type="": objs.__setitem__(f"{area}:{key}", data))
    monkeypatch.setattr(storage, "get", lambda area, key: objs[f"{area}:{key}"])
    return objs


def _seed(db) -> None:
    gap_pool.record(db, [Signal(URL1, "external", "EV-M1", impact=5, authority=1),
                         Signal(URL2, "external", "EV-M2", impact=3, authority=1)])
    gap_pool.run_week(db, TODAY)


def _return_batch(tmp_path: Path, failed: list[dict]) -> Path:
    b = tmp_path / "ret"
    b.mkdir()
    (b / "manifest.json").write_text(json.dumps({"files": [], "failed": failed}), encoding="utf-8")
    return b


def test_reason_mapping():
    assert acq.manual_reason("HTTP 403") == "blocked"
    assert acq.manual_reason("Name or service not known") == "dns"
    assert acq.manual_reason("timeout x3") == "timeout"
    assert acq.manual_reason("html only") == "no_file"
    assert acq.manual_reason("whatever") == "other"
    assert set(acq.MANUAL_REASONS) >= {"blocked", "dns", "timeout", "no_file", "not_found", "other"}


def test_failed_return_marks_manual_and_week_keeps_it(tmp_path):
    with SessionLocal() as db:
        _seed(db)
        assert {g.plan for g in db.scalars(select(KbGap))} == {"direct"}
        report, closed = gap_pool.import_return(db, _return_batch(tmp_path, [{"url": URL1, "reason": "HTTP 403"},
                                                                            {"url": f"{HOST}/act/none", "reason": "x"}]))
        assert report.ok and closed == 0 and report.manual == 1
        g = db.scalar(select(KbGap).where(KbGap.gap_key == URL1))
        assert (g.plan, g.manual_reason, g.status) == ("manual", "blocked", "open")
        out = gap_pool.run_week(db, TODAY)
        db.refresh(g)
        assert g.plan == "manual" and out["manual"] == 1
        assert URL1 not in [t["url"] for t in gap_pool.dispatch_package(db)["tasks"]]
        a = db.scalar(select(AuditLog).where(AuditLog.action == "kb.gap.manual").order_by(AuditLog.id.desc()))
        assert a is not None and "pdpa" not in json.dumps(a.detail, ensure_ascii=False)


def test_mark_manual_creates_named_gap_for_backlog():
    with SessionLocal() as db:
        g = gap_pool.mark_manual(db, "埃及 Law No. 151 of 2020", "not_found", create=True, event_ref="BACKLOG-1")
        assert (g.plan, g.manual_reason, g.status) == ("manual", "not_found", "open")
        with pytest.raises(ValueError):
            gap_pool.mark_manual(db, URL1, "bogus", create=True, event_ref="BACKLOG-2")
        q = gap_pool.manual_queue(db)
        item = next(i for i in q if i["gap_key"].startswith("埃及"))
        assert item["is_url"] is False and item["reason_label"] == acq.MANUAL_REASONS["not_found"]


def test_queue_and_upload_permissions(people, tmp_path):
    with SessionLocal() as db:
        _seed(db)
        gap_pool.import_return(db, _return_batch(tmp_path, [{"url": URL1, "reason": "403"}]))
        gid = db.scalar(select(KbGap.id).where(KbGap.gap_key == URL1))
    assert people["consultant"].get("/api/kb/sources/manual-queue").status_code == 403
    r = people["expert"].get("/api/kb/sources/manual-queue")
    assert r.status_code == 200 and [i["id"] for i in r.json()] == [gid]
    assert r.json()[0]["in_whitelist"] is True
    files = {"file": ("a.pdf", b"%PDF-1.4 x", "application/pdf")}
    r = people["consultant"].post(f"/api/kb/sources/manual-queue/{gid}/upload", files=files, data={"title": "示范法"})
    assert r.status_code == 403


def test_upload_rejects_url_outside_active_sources(people, fake_storage):
    with SessionLocal() as db:
        g = gap_pool.mark_manual(db, "示范国网络安全法", "blocked", create=True, event_ref="BACKLOG-3")
        gid = g.id
    files = {"file": ("a.pdf", b"%PDF-1.4 x", "application/pdf")}
    r = people["expert"].post(f"/api/kb/sources/manual-queue/{gid}/upload", files=files, data={"title": "示范法"})
    assert r.status_code == 422          # 编号类缺口没给网址
    r = people["expert"].post(f"/api/kb/sources/manual-queue/{gid}/upload", files=files,
                              data={"title": "示范法", "url": "https://other.example.org/a.pdf"})
    assert r.status_code == 422 and "正式来源" in r.json()["detail"]
    assert not fake_storage


def test_upload_runs_publish_and_closes_gap(people, fake_storage, monkeypatch, tmp_path):
    seen: dict = {}

    def fake_run(db, user, manifest, src_dir):
        seen["csv"] = manifest.read_text(encoding="utf-8")
        seen["files"] = sorted(p.name for p in src_dir.iterdir())
        seen["user"] = user.username
        return SimpleNamespace(docs=[SimpleNamespace(published=3, manual=0, rejected=0)], sampled=[], suspended="",
                               skipped=[])

    monkeypatch.setattr(gap_manual.autopublish, "run", fake_run)
    queued: list = []
    monkeypatch.setattr(gap_manual.jobs, "enqueue", lambda db, kind, payload, **kw: queued.append((kind, payload)) or 7)
    with SessionLocal() as db:
        _seed(db)
        gap_pool.import_return(db, _return_batch(tmp_path, [{"url": URL1, "reason": "403"}]))
        gid = db.scalar(select(KbGap.id).where(KbGap.gap_key == URL1))
    data = b"%PDF-1.4 demo law"
    files = {"file": ("任意名.pdf", data, "application/pdf")}
    r = people["expert"].post(f"/api/kb/sources/manual-queue/{gid}/upload", files=files,
                              data={"title": "示范国个人数据保护法"})
    assert r.status_code == 202, r.text
    assert r.json()["job_id"] == 7
    # 上传后不再出现在待下载清单里（处理中），也不能重复上传
    assert [i for i in people["expert"].get("/api/kb/sources/manual-queue").json() if i["id"] == gid][0]["plan"] == "uploaded"
    assert people["expert"].post(f"/api/kb/sources/manual-queue/{gid}/upload", files=files,
                                 data={"title": "x"}).status_code == 409
    kind, payload = queued[0]
    assert kind == gap_manual.JOB_KIND and "任意名" not in json.dumps(payload, ensure_ascii=False)
    gap_manual._job(payload, None)
    sha = hashlib.sha256(data).hexdigest()
    assert sha in seen["csv"] and "示范国个人数据保护法" in seen["csv"] and "示范国总检察署" in seen["csv"]
    assert URL1 in seen["csv"] and "manifest.json" in seen["files"]
    assert seen["user"] == people["_names"]["expert"]
    with SessionLocal() as db:
        g = db.get(KbGap, gid)
        assert (g.status, g.close_reason) == ("closed", "collected")
    # 已入库后再传同一缺口：409「原件已上传」，不是 404（#459 测试评论）
    r = people["expert"].post(f"/api/kb/sources/manual-queue/{gid}/upload", files=files, data={"title": "x"})
    assert r.status_code == 409 and "已上传" in r.text, r.text
    with SessionLocal() as db:
        a = db.scalar(select(AuditLog).where(AuditLog.action == "kb.gap.manual_upload").order_by(AuditLog.id.desc()))
        assert a is not None and "任意名" not in json.dumps(a.detail, ensure_ascii=False)


def test_job_failure_returns_gap_to_manual(people, fake_storage, monkeypatch, tmp_path):
    def boom(db, user, manifest, src_dir):
        raise RuntimeError("解析失败")

    monkeypatch.setattr(gap_manual.autopublish, "run", boom)
    queued: list = []
    monkeypatch.setattr(gap_manual.jobs, "enqueue", lambda db, kind, payload, **kw: queued.append(payload) or 1)
    with SessionLocal() as db:
        _seed(db)
        gap_pool.import_return(db, _return_batch(tmp_path, [{"url": URL2, "reason": "dns"}]))
        gid = db.scalar(select(KbGap.id).where(KbGap.gap_key == URL2))
    r = people["admin"].post(f"/api/kb/sources/manual-queue/{gid}/upload",
                             files={"file": ("b.pdf", b"%PDF-1.4 b", "application/pdf")}, data={"title": "示范网安法"})
    assert r.status_code == 202
    with pytest.raises(RuntimeError):
        gap_manual._job(queued[0], None)
    with SessionLocal() as db:
        g = db.get(KbGap, gid)
        assert (g.status, g.plan) == ("open", "manual")
