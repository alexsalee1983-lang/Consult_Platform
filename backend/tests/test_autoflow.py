"""交付流程自动推进（二期 C5：P6、P8、P10）：证据齐了且安静一段时间后自动发起交付链（只发起一次），
交付链写完立即自动评审，顾问手改后闲置 24 小时自动评审（同一修订只排一次），门禁通过自动提交签署（提交人 platform），
门禁不过不提交，开关关闭时什么也不做；审计记规则名，不记原文。"""
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal, utcnow
from app.main import app
from app.models import AgentRun, AuditLog, Job, Project, QualityReview, SignOff
from app.project import autoflow, deliverables, quality
from app.services import config_registry, gateway

from . import test_quality as tq
from . import test_runs as tr
from .conftest import create_user, logged_in_client


def _model(monkeypatch, m):
    monkeypatch.setattr(get_settings(), "auto_flow", True)
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
    monkeypatch.setattr(get_settings(), "llm_max_retries", 0)
    gateway.set_transport(httpx.MockTransport(m.handler))
    return m


@pytest.fixture()
def chain_model(monkeypatch):
    yield _model(monkeypatch, tr._Model())
    gateway.set_transport(None)


@pytest.fixture()
def review_model(monkeypatch):
    m = _model(monkeypatch, tq._Model())
    code = tq._new_project()
    clients = [logged_in_client(app, create_user(("consultant",), {code: role})) for role in ("member", "manager")]
    yield m, code, *clients
    for c in clients:
        c.__exit__(None, None, None)
    gateway.set_transport(None)


def _project(db, code: str) -> Project:
    return db.scalar(select(Project).where(Project.code == code))


def _triggers(code: str, action: str) -> list[str]:
    with SessionLocal() as db:
        logs = db.scalars(select(AuditLog).where(AuditLog.project_code == code, AuditLog.action == action)).all()
        return [json.loads(a.detail).get("trigger", "") for a in logs]


def test_auto_start_once_after_quiet_then_reviews_chain_output(chain_model):
    code = tr._new_project()
    account = create_user(("consultant",), {code: "member"})
    user = account[0]
    with logged_in_client(app, account) as c:
        with SessionLocal() as db:
            assert autoflow.scan_runs(db, _project(db, code), now=utcnow() + timedelta(hours=3)) == []  # 没有证据
        tr._note(c, code)
        with SessionLocal() as db:
            p = _project(db, code)
            assert autoflow.scan_runs(db, p) == []                                    # 刚传完：等一批传完
            assert autoflow.scan_runs(db, p, now=utcnow() + timedelta(hours=3)) == [code]
            run = db.scalar(select(AgentRun).where(AgentRun.project_id == p.id))
            assert run.created_by == user and run.status == "queued"
            assert autoflow.scan_runs(db, p, now=utcnow() + timedelta(hours=9)) == []   # 只发起一次
            rid = run.id
        assert _triggers(code, "run.start") == [autoflow.RULE_RUN]
        with SessionLocal() as db:                                                   # G3①：运行记生效配置哈希
            log = db.scalar(select(AuditLog).where(AuditLog.project_code == code, AuditLog.action == "run.start"))
            assert json.loads(log.detail)["config_registry_sha"] == config_registry.effective_sha()
        tr._work()
        assert tr._run(c, code, rid)["status"] == "done"
    with SessionLocal() as db:                                                       # 交付链写完：立即排评审
        p = _project(db, code)
        rs = db.scalars(select(QualityReview).where(QualityReview.project_id == p.id)).all()
        codes = {r.deliverable_id: d.code for d in p.deliverables for r in rs if r.deliverable_id == d.id}
        assert sorted(codes.values()) == ["D3", "D4", "D5", "D6", "D7", "D8"]
        assert {r.created_by for r in rs} == {user} and all(r.status == "queued" for r in rs)
        assert autoflow.scan_reviews(db, p, now=utcnow() + timedelta(hours=30)) == []   # 同一修订不再排
        for r in rs:                                                                 # 不留给其他用例的 worker
            r.status = "failed"
        db.query(Job).filter(Job.kind == "quality.review", Job.project_code == code).delete()
        db.commit()
    assert set(_triggers(code, "quality.request")) == {autoflow.RULE_REVIEW_CHAIN}


def test_idle_review_then_gate_passes_auto_submit(review_model, monkeypatch):
    _, code, member, manager = review_model
    monkeypatch.setattr(get_settings(), "auto_submit_deliverables", "D4")   # 把 D4 调到 L3 才自动提交
    tq._draft(member, code)
    with SessionLocal() as db:
        p = _project(db, code)
        assert autoflow.scan_runs(db, p, now=utcnow() + timedelta(hours=3)) == []   # 已经手写了交付件：不自动发起
        assert autoflow.scan_reviews(db, p) == []                                     # 顾问刚改过：先不评
        assert autoflow.scan_reviews(db, p, now=utcnow() + timedelta(hours=25)) == [f"{code}/D4"]
        assert autoflow.scan_reviews(db, p, now=utcnow() + timedelta(hours=26)) == []  # 已有进行中的评审
    tq._work()
    with SessionLocal() as db:
        p = _project(db, code)
        d = deliverables.get_deliverable(db, p, "D4")
        assert d.status == "review"                                                  # 门禁通过：自动待签署
        s = db.scalar(select(SignOff).where(SignOff.deliverable_id == d.id))
        assert s.action == "submit" and s.actor == "platform" and "门禁" in s.comment
        assert autoflow.scan_reviews(db, p, now=utcnow() + timedelta(hours=60)) == []
    assert _triggers(code, "quality.request") == [autoflow.RULE_REVIEW_IDLE]
    assert _triggers(code, "deliverable.submit") == [autoflow.RULE_SUBMIT]
    r = manager.post(tq._url(code, "signoff/sign"), json={"comment": "同意"})          # 签署仍由项目经理
    assert r.status_code == 200 and r.json()["status"] == "signed", r.text


def test_l2_deliverable_not_auto_submitted(review_model):
    """决策记录 2026-11-18：D3、D4、D5、D7 为 L2，门禁通过也不自动提交，由顾问看过标出的问题后提交。"""
    _, code, member, _ = review_model
    assert set(autoflow.auto_submit_codes()) == {"D1", "D2", "D6", "D8"}
    tq._draft(member, code)
    with SessionLocal() as db:
        p = _project(db, code)
        assert autoflow.scan_reviews(db, p, now=utcnow() + timedelta(hours=25)) == [f"{code}/D4"]
    tq._work()
    with SessionLocal() as db:
        p = _project(db, code)
        d = deliverables.get_deliverable(db, p, "D4")
        assert d.status == "drafting" and quality.gate(db, d).ok                     # 门禁通过但不提交
        assert db.scalar(select(SignOff).where(SignOff.deliverable_id == d.id)) is None
    assert _triggers(code, "deliverable.submit") == []
    r = member.post(tq._url(code, "signoff/submit"), json={"comment": "看过标出的问题"})
    assert r.status_code == 200 and r.json()["status"] == "review", r.text


def test_blocked_review_not_submitted_and_switch_off(review_model, monkeypatch):
    m, code, member, _ = review_model
    m.reply = tq.REVIEW.replace("阻断 0 项", "阻断 1 项")
    tq._draft(member, code)
    assert member.post(tq._url(code, "reviews")).status_code == 202
    tq._work()
    with SessionLocal() as db:
        p = _project(db, code)
        assert deliverables.get_deliverable(db, p, "D4").status == "drafting"         # 门禁不过：不提交
    assert member.put(tq._url(code, "sections/1"), json={"body": tq.BODY1 + "补充。"}).status_code == 201
    monkeypatch.setattr(get_settings(), "auto_flow", False)
    with SessionLocal() as db:
        p = _project(db, code)
        assert autoflow.scan_reviews(db, p, now=utcnow() + timedelta(hours=48)) == []
        assert autoflow.scan_runs(db, p, now=utcnow() + timedelta(hours=48)) == []
