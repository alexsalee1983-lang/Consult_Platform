"""四期 F3②：质量事件与平台联动（4A 架构设计 6.3）。

- 遏制“冻结导出”：事件挂交付件、未关闭时，该交付件导出（Word / PPT）一律 409，审计记 blocked；复盘关闭后恢复；
- 程序自动登记：知识专家抽检自动发布的现行版本判不通过时，平台自动登记一条“事实与证据不符”事件挂该条目，
  登记人记为 system:kb.sample，抽检意见只记长度和哈希；
- 影响面反查：事件挂知识条目时，详情列出当前正文引用了该条目的交付件（项目负责人看全部，项目经理只看自己项目）。
"""
from __future__ import annotations

import hashlib

from sqlalchemy import select

from app.core.db import SessionLocal
from app.kb import autopublish
from app.main import app
from app.models import AuditLog, KbItem, KbVersion, QualityIncident

from .conftest import create_user, logged_in_client
from .test_deliverables import _base, _new_project, _publish_kb
from .test_kb_autopublish import LAW, _batch, _users


def _incident(c, code: str, **kw) -> int:
    body = {"category": "repealed_basis", "project": code, "ref_kind": "deliverable", "ref_id": "D4", **kw}
    r = c.post("/api/quality-incidents", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _step(c, iid: int, step: str, **kw):
    r = c.post(f"/api/quality-incidents/{iid}/events", json={"step": step, **kw})
    assert r.status_code == 201, r.text


def test_freeze_export_blocks_until_closed():
    code = _new_project()
    c = logged_in_client(app, create_user(("consultant",), {code: "manager"}))
    c.put(f"{_base(code)}/outline", json={"titles": ["现状梳理"]})
    assert c.put(f"{_base(code)}/sections/1", json={"body": "现状描述。"}).status_code == 201
    assert c.get(f"{_base(code)}/export").status_code == 200

    iid = _incident(c, code)
    assert c.get(f"{_base(code)}/export").status_code == 200           # 只登记、未遏制：不冻结
    _step(c, iid, "grade", level="P1")
    _step(c, iid, "contain", action="freeze_export")
    for fmt in ("docx", "pptx"):
        r = c.get(f"{_base(code)}/export", params={"format": fmt})
        assert r.status_code == 409 and f"#{iid}" in r.json()["detail"]
    with SessionLocal() as db:
        a = db.scalars(select(AuditLog).where(AuditLog.action == "deliverable.export", AuditLog.result == "blocked")
                       .order_by(AuditLog.id.desc())).first()
        assert a is not None and "quality_incident_frozen" in a.detail
    other = _new_project()                                              # 别的项目同号交付件不受影响
    c2 = logged_in_client(app, create_user(("consultant",), {other: "manager"}))
    c2.put(f"{_base(other)}/outline", json={"titles": ["现状梳理"]})
    c2.put(f"{_base(other)}/sections/1", json={"body": "现状描述。"})
    assert c2.get(f"{_base(other)}/export").status_code == 200

    for s in ("assess", "correct"):
        _step(c, iid, s)
    _step(c, iid, "root_cause", root_cause="knowledge")
    _step(c, iid, "prevent")
    _step(c, iid, "review")
    assert c.get(f"{_base(code)}/export").status_code == 200           # 复盘关闭后解冻


def test_sample_fail_auto_registers_incident(tmp_path, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "kb_sample_fail_max", 1.0)
    monkeypatch.setattr(get_settings(), "kb_sample_confidence", 0.3)
    op, checker = _users()
    manifest, src, _ = _batch(tmp_path, [("ok.txt", LAW, {})])
    comment = "第二条与原件不一致，原件写的是年度评估"
    with SessionLocal() as db:
        r = autopublish.run(db, db.merge(op), manifest, src)
        doc_id = r.sampled[0]
        item = db.scalar(select(KbItem).where(KbItem.doc_id == doc_id))
        autopublish.sample_check(db, db.merge(checker), db.get(KbVersion, item.current_version_id), "fail", comment)
        inc = db.scalars(select(QualityIncident).where(QualityIncident.ref_kind == "kb_item",
                                                       QualityIncident.ref_id == doc_id)).one()
        assert inc.category == "fact_mismatch" and inc.reported_by == "system:kb.sample"
        assert inc.project_code == "" and inc.description_len == len(comment)
        assert inc.description_sha == hashlib.sha256(comment.encode()).hexdigest()[:16]
        a = db.scalars(select(AuditLog).where(AuditLog.action == "quality_incident.register")
                       .order_by(AuditLog.id.desc())).first()
        assert comment not in str(a.detail) and a.actor == "system:kb.sample"


def test_sample_pass_registers_nothing(tmp_path, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "kb_sample_fail_max", 1.0)
    monkeypatch.setattr(get_settings(), "kb_sample_confidence", 0.3)
    op, checker = _users()
    manifest, src, _ = _batch(tmp_path, [("ok.txt", LAW, {})])
    with SessionLocal() as db:
        r = autopublish.run(db, db.merge(op), manifest, src)
        doc_id = r.sampled[0]
        item = db.scalar(select(KbItem).where(KbItem.doc_id == doc_id))
        autopublish.sample_check(db, db.merge(checker), db.get(KbVersion, item.current_version_id), "pass")
        assert db.scalar(select(QualityIncident).where(QualityIncident.ref_id == doc_id)) is None


def test_impact_lists_citing_deliverables_by_visibility():
    doc_id = _publish_kb("质量事件影响面测试依据")
    a, b = _new_project(), _new_project()
    for code in (a, b):
        w = logged_in_client(app, create_user(("consultant",), {code: "member"}))
        w.put(f"{_base(code)}/outline", json={"titles": ["差距分析"]})
        r = w.put(f"{_base(code)}/sections/1", json={"body": f"依据【依据：GB/T 22239 2019 8.1.5｜{doc_id}】。"})
        assert r.status_code == 201
    lead = logged_in_client(app, create_user(("project_lead",), {a: "manager"}))
    iid = _incident(lead, a, ref_kind="kb_item", ref_id=doc_id, category="fabricated_citation")
    got = lead.get(f"/api/quality-incidents/{iid}").json()["impact"]
    assert {(x["project"], x["deliverable"]) for x in got} >= {(a, "D4"), (b, "D4")}

    mgr = logged_in_client(app, create_user(("consultant",), {a: "manager"}))
    got = mgr.get(f"/api/quality-incidents/{iid}").json()["impact"]
    assert {x["project"] for x in got} == {a}                           # 项目经理看不到别的项目
