"""三期 B25②：知识审批队列“来源确认”类。

本周计划落在 confirm 的缺口进队列；知识管理员按来源批量确认或不做；确认的缺口不绕过白名单，
等对应来源转正式后才出采集任务（下发包）。
"""
from datetime import date

import pytest
from sqlalchemy import delete, select

from app.core.db import SessionLocal
from app.core.permissions import CONSULTANT, KNOWLEDGE_ADMIN, KNOWLEDGE_EXPERT
from app.kb import gap_pool
from app.kb.acquisition import Signal
from app.main import app
from app.models import AuditLog, KbGap, KbSource

from .conftest import create_user, logged_in_client

TODAY = date(2026, 12, 7)
HOST = "https://sc.example.gov.cn"
SRC = {"name": "测试来源：监管通报栏目", "url": f"{HOST}/notice/", "publisher": "测试监管机构",
       "license": "公开发布，可内部引用并注明出处", "authority_kind": "监管通报"}


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
            db.execute(delete(KbSource).where(KbSource.url.like(f"{HOST}/%")))
            db.commit()
    wipe()
    yield
    wipe()


def _seed() -> dict[str, int]:
    with SessionLocal() as db:
        gap_pool.record(db, [Signal(f"{HOST}/notice/2026-1", "external", "EV-S1", impact=5, authority=1),
                             Signal(f"{HOST}/notice/2026-2", "external", "EV-S2", impact=3, authority=1),
                             Signal("GB/T 22239", "usage", "EV-S3", impact=2)])
        gap_pool.run_week(db, TODAY)
        return {g.gap_key: g.id for g in db.scalars(select(KbGap))}


def test_queue_lists_confirm_lane_grouped_by_host(people):
    ids = _seed()
    r = people["expert"].get("/api/kb/sources/confirm-queue")
    assert r.status_code == 200, r.text
    groups = {g["group"]: g for g in r.json()}
    assert set(groups) == {"sc.example.gov.cn", "编号"}
    assert [i["id"] for i in groups["sc.example.gov.cn"]["items"]] == [ids[f"{HOST}/notice/2026-1"],
                                                                       ids[f"{HOST}/notice/2026-2"]]
    assert groups["编号"]["items"][0]["gap_key"] == "GB/T 22239"
    assert people["consultant"].get("/api/kb/sources/confirm-queue").status_code == 403


def test_only_admin_decides_and_reject_closes(people):
    ids = _seed()
    gid = ids["GB/T 22239"]
    body = {"gap_ids": [gid], "decision": "reject"}
    assert people["expert"].post("/api/kb/sources/confirm-queue/decide", json=body).status_code == 403
    assert people["consultant"].post("/api/kb/sources/confirm-queue/decide", json=body).status_code == 403
    r = people["admin"].post("/api/kb/sources/confirm-queue/decide", json=body)
    assert r.status_code == 200, r.text
    assert r.json() == {"decided": 1, "source": None}
    with SessionLocal() as db:
        g = db.get(KbGap, gid)
        assert g.status == "closed" and g.close_reason == "dropped"
    # 已不在队列里的缺口再决定 → 422
    assert people["admin"].post("/api/kb/sources/confirm-queue/decide", json=body).status_code == 422


def test_approve_by_source_registers_candidate_and_waits_for_whitelist(people):
    ids = _seed()
    urls = [f"{HOST}/notice/2026-1", f"{HOST}/notice/2026-2"]
    body = {"gap_ids": [ids[u] for u in urls], "decision": "approve", "source": SRC}
    r = people["admin"].post("/api/kb/sources/confirm-queue/decide", json=body)
    assert r.status_code == 200, r.text
    code = r.json()["source"]
    assert r.json()["decided"] == 2 and code.startswith("SR-")
    with SessionLocal() as db:
        src = db.scalar(select(KbSource).where(KbSource.code == code))
        assert src.status == "candidate" and src.created_by == people["_names"]["admin"]
        assert {g.plan for g in db.scalars(select(KbGap).where(KbGap.gap_key.in_(urls)))} == {"confirmed"}
        # 来源还没转正式：每周任务不把它们挪回 confirm，也不出下发包
        gap_pool.run_week(db, TODAY)
        assert {g.plan for g in db.scalars(select(KbGap).where(KbGap.gap_key.in_(urls)))} == {"confirmed"}
        assert gap_pool.dispatch_package(db)["tasks"] == []
        # 来源转正式（另一人页面批准，这里直接改状态模拟）→ 下周进 direct、出下发包
        src.status = "active"
        db.commit()
        gap_pool.run_week(db, TODAY)
        assert {g.plan for g in db.scalars(select(KbGap).where(KbGap.gap_key.in_(urls)))} == {"direct"}
        assert sorted(t["url"] for t in gap_pool.dispatch_package(db)["tasks"]) == urls
        a = db.scalar(select(AuditLog).where(AuditLog.action == "kb.gap.confirm").order_by(AuditLog.id.desc()))
        assert a is not None and "notice" not in str(a.detail)          # 审计不记网址原文


def test_approve_source_must_cover_urls(people):
    ids = _seed()
    body = {"gap_ids": [ids[f"{HOST}/notice/2026-1"]], "decision": "approve",
            "source": {**SRC, "url": "https://other.example.gov.cn/"}}
    assert people["admin"].post("/api/kb/sources/confirm-queue/decide", json=body).status_code == 422
    # 不带来源也可以确认（之后再登记来源）
    body = {"gap_ids": [ids["GB/T 22239"]], "decision": "approve"}
    r = people["admin"].post("/api/kb/sources/confirm-queue/decide", json=body)
    assert r.status_code == 200 and r.json() == {"decided": 1, "source": None}
