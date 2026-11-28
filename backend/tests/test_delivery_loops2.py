"""四期 A31 ⑨⑪⑫：对外文本硬规则、网关优先级、上游变更挂钩。"""
from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.main import app
from app.models import DeliverableSection, Project
from app.project import loops, outward
from app.services import gateway

from .conftest import create_user, logged_in_client
from .test_runs import _new_project


# ---- ⑨ 对外文本 ----

def test_outward_check_flags_qualification_and_secrets():
    assert outward.check("本次差距分析给出整改建议。") == []
    assert "资质边界用语" in outward.check("该系统已通过等保三级测评。")
    assert outward.check("本报告不构成测评结论。") == []
    assert any(h != "资质边界用语" for h in outward.check("管理口令 password=Abc12345!x"))


@pytest.fixture()
def pm():
    code = _new_project()
    user = create_user(("consultant",), {code: "manager"})
    c = logged_in_client(app, user)
    yield code, c, user[0]
    c.__exit__(None, None, None)


def test_export_blocked_when_body_has_qualification_wording(pm):
    code, c, _ = pm
    from app.project import deliverables
    from app.models import User
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        d = sorted(p.deliverables, key=lambda x: x.id)[1]
        db.add(DeliverableSection(deliverable_id=d.id, seq=1, title="结论", created_by="t"))
        db.commit()
        dcode = d.code
        u = db.scalar(select(User).where(User.username == pm[2]))
        deliverables.write_section(db, u, p, d, 1, "经评估，该系统已通过等保三级测评。")
    r = c.get(f"/api/projects/{code}/deliverables/{dcode}/export")
    assert r.status_code == 422 and "资质边界" in r.json()["detail"]


# ---- ⑪ 网关优先级 ----

def test_priority_gate_serves_chat_before_delivery_before_kb():
    gate = gateway.PriorityGate(1)
    order: list[str] = []
    gate.acquire("delivery")                 # 占住唯一的并发位

    def worker(lane: str) -> None:
        gate.acquire(lane)
        order.append(lane)
        gate.release()

    ts = []
    for lane in ("kb", "delivery", "chat"):
        t = threading.Thread(target=worker, args=(lane,))
        t.start()
        ts.append(t)
        time.sleep(0.05)
    gate.release()
    for t in ts:
        t.join(2)
    assert order == ["chat", "delivery", "kb"]


def test_unlimited_gate_never_blocks_and_lane_context():
    gate = gateway.PriorityGate(0)
    for _ in range(5):
        gate.acquire("kb")
    assert gateway.current_lane() == "delivery"
    with gateway.lane("kb"):
        assert gateway.current_lane() == "kb"
    assert gateway.current_lane() == "delivery"


# ---- ⑫ 上游变更 → 下游重审 ----

def test_upstream_change_marks_written_sections_and_rewrite_clears(pm):
    code, _, username = pm
    from app.models import User
    from app.project import deliverables
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        d = sorted(p.deliverables, key=lambda x: x.id)[1]
        db.add_all([DeliverableSection(deliverable_id=d.id, seq=1, title="背景", created_by="t"),
                    DeliverableSection(deliverable_id=d.id, seq=2, title="现状", created_by="t")])
        db.commit()
        u = db.scalar(select(User).where(User.username == username))
        deliverables.write_section(db, u, p, d, 1, "背景正文")
        n = loops.on_upstream_change(db, p, "customization", [(d.code, None)])
        db.commit()
        assert n == 1                         # 只标写过正文的章
        s1, s2 = sorted(d.sections, key=lambda s: s.seq)
        assert s1.stale_reason == "customization" and s1.stale_at is not None and s2.stale_reason == ""
        assert loops.on_upstream_change(db, p, "customization", [(d.code, 1)]) == 0   # 已标的不重复计
        deliverables.write_section(db, u, p, d, 1, "按新定制项改写")
        assert s1.stale_reason == ""


# ---- ⑩ 无正文漂移指标（JUDGMENT_CAPTURE 关闭时） ----

def test_drift_weekly_without_body():
    from datetime import datetime, timedelta, timezone

    from app.models import Confirmation, QualityReview, SectionVersion
    from app.services import indicators_drift

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        p = db.scalars(select(Project).where(Project.code == _new_project())).one()
        for choice in ("A", "A", "B", ""):
            db.add(Confirmation(project_id=p.id, kind="scope", title="t", agent="a", status="resolved",
                                options=[{"key": "A", "text": "x"}, {"key": "B", "text": "y"}], recommendation="A",
                                choice=choice, resolved_at=now))
        d = p.deliverables[0] if p.deliverables else None
        if d is None:
            from app.models import Deliverable
            d = Deliverable(project_id=p.id, code="D9", title="t", status="draft", revision=1)
            db.add(d)
            db.flush()
        s = DeliverableSection(deliverable_id=d.id, seq=1, title="x", current_version=2, created_by="t")
        db.add(s)
        db.flush()
        db.add(SectionVersion(section_id=s.id, version=1, body="a" * 100, char_count=100, body_sha="1", author="t",
                              agent_code="w", created_at=now))
        db.add(SectionVersion(section_id=s.id, version=2, body="a" * 50, char_count=50, body_sha="2", author="u",
                              agent_code="", created_at=now))
        db.add(QualityReview(project_id=p.id, deliverable_id=d.id, revision=1, status="done", blocking=3,
                             created_by="t", created_at=now))
        db.commit()
        rep = indicators_drift.weekly(db, now - timedelta(days=7), now + timedelta(minutes=1), [p.id])
    w = rep["weeks"][-1]
    assert w["cards"] == {"resolved": 4, "chose": 3, "followed": 2, "recommend_rate": round(2 / 3, 4)}
    assert w["review_blocking"] == 3
    assert w["edits"] == {"light": 0, "moderate": 0, "heavy": 1}     # 100 -> 50 字，幅度 0.5
    assert "body" not in str(rep)


def test_drift_api_lead_only():
    lead = create_user(("consultant", "project_lead"), {})
    mgr = create_user(("consultant",), {})
    with logged_in_client(app, lead) as c:
        r = c.get("/api/indicators/drift?weeks=4")
        assert r.status_code == 200 and len(r.json()["weeks"]) == 4
        assert c.get("/api/indicators/drift?weeks=0").status_code == 422
    with logged_in_client(app, mgr) as c:
        assert c.get("/api/indicators/drift").status_code == 403
