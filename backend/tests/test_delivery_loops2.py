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
