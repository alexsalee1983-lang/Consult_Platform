"""三期 F2①：指标精简版 10 项 + 自动发布率、人工分钟 / 百条、国标内容引用（单列）与看板 v1。

只出数字，不出项目代号、标题、正文；只给项目负责人。"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.db import SessionLocal
from app.main import app
from app.models import (Deliverable, DeliverableSection, Evidence, JudgmentEvent, KbItem, KbVersion, Project, SectionVersion,
                        SignOff)
from app.core import audit
from app.services import indicators

from .conftest import create_user, logged_in_client
from .test_kb_citations import library  # noqa: F401  （依据原文：示范引用法第一、二条，GB/T 99998-2029 条款目录）

NOW = datetime.now(timezone.utc)
KEYS = {"qualified_deliverables", "fabricated_clauses", "gb_citations", "eval_accuracy", "gate_first_pass",
        "advice_edit_magnitude", "client_review_rounds", "draft_hours", "fact_sheet_edit_rate",
        "effective_citations", "expert_on_time", "auto_publish_rate", "human_minutes_per_100"}


def _sign(db, p: Project, code: str, titles_bodies: list[tuple[str, str]], flow: list[str]) -> Deliverable:
    d = Deliverable(code=code, title=f"{code} 示范", status="signed", revision=len(titles_bodies))
    p.deliverables.append(d)
    db.flush()
    versions = {}
    for seq, (title, body) in enumerate(titles_bodies, start=1):
        s = DeliverableSection(deliverable_id=d.id, seq=seq, title=title, current_version=1, created_by="t")
        db.add(s)
        db.flush()
        db.add(SectionVersion(section_id=s.id, version=1, body=body, char_count=len(body), body_sha="x", author="t",
                              agent_code="writer", created_at=NOW - timedelta(hours=2)))
        versions[str(seq)] = 1
    for action in flow:
        db.add(SignOff(project_id=p.id, deliverable_id=d.id, action=action, revision=d.revision, versions=versions,
                       actor="t", created_at=NOW - timedelta(hours=1)))
    db.flush()
    return d


def _project(db) -> Project:
    p = Project(code=f"MET-{secrets.token_hex(3).upper()}", service="SOC 运营体系设计", industry="银行",
                tier="standard")
    db.add(p)
    db.flush()
    db.add(Evidence(project_id=p.id, code="O-01", kind="observation", seq=1, title="值班室", content="大屏两块",
                    char_count=4, created_by="t", created_at=NOW - timedelta(hours=12)))
    return p


def _by_key(rep: dict) -> dict:
    return {m["key"]: m for m in rep["metrics"]}


def test_deliverable_metrics(library):  # noqa: F811
    with SessionLocal() as db:
        doc_id = db.scalar(select(KbItem.doc_id).join(KbVersion, KbVersion.id == KbItem.current_version_id).where(
            KbItem.retired.is_(False), KbItem.doc_type == "SRC", KbVersion.title.like("%示范引用法%第二条%")))
        assert doc_id
        p = _project(db)
        # D1：一次通过，但编了条款号（示范引用法没有第九条），另引了一处国标
        _sign(db, p, "D1", [("现状", "依照《示范引用法》第九条处理 [O-01]。"),
                            ("整改建议", "按 GB/T 99998-2029 8.1.4.1 做身份鉴别。")], ["submit", "sign"])
        # D2：干净，但被退回过一次
        _sign(db, p, "D2", [("差距", "值班台无交接记录 [O-01]。")], ["submit", "return", "submit", "sign"])
        # D3：干净、一次通过，引用知识条目
        _sign(db, p, "D3", [("整改建议", f"按《示范引用法》第二条落实 [O-01]【依据｜{doc_id}】。")],
              ["submit", "sign"])
        sec = db.scalar(select(DeliverableSection).join(Deliverable).where(
            Deliverable.project_id == p.id, Deliverable.code == "D1", DeliverableSection.seq == 2))
        db.add_all([JudgmentEvent(project_id=p.id, kind="edit", deliverable_code="D1", section_seq=2,
                                  category="moderate", magnitude=0.4, created_at=NOW),
                    JudgmentEvent(project_id=p.id, kind="edit", deliverable_code="D1", section_seq=1,
                                  category="light", magnitude=0.1, created_at=NOW)])
        assert sec is not None
        db.commit()
        rep = indicators.compute(db, NOW - timedelta(days=1), NOW + timedelta(minutes=1), project_ids=[p.id])
    m = _by_key(rep)
    assert set(m) == KEYS
    assert m["gate_first_pass"]["value"] == round(2 / 3, 4) and m["gate_first_pass"]["detail"]["signed"] == 3
    assert m["fabricated_clauses"]["detail"]["deliverables"] == 1 and m["fabricated_clauses"]["status"] == "breach"
    assert m["fabricated_clauses"]["redline"] is True
    assert m["gb_citations"]["value"] == 1
    assert m["qualified_deliverables"]["value"] == 1                 # 只有 D3：D1 编造条款号，D2 被退回过
    assert m["effective_citations"]["value"] == 1
    assert m["advice_edit_magnitude"]["value"] == 0.4                # 只算建议类章节
    assert m["advice_edit_magnitude"]["detail"]["all_sections"] == 0.25
    assert m["draft_hours"]["value"] == 10.0                         # 最后一份证据到初稿齐
    for k in ("client_review_rounds", "fact_sheet_edit_rate", "expert_on_time"):
        assert m[k]["status"] == "na" and m[k]["value"] is None and m[k]["note"]
    text = json.dumps(rep, ensure_ascii=False)
    assert p.code not in text and "示范引用法" not in text and "值班台" not in text   # 不出项目代号和正文


def test_kb_and_eval_metrics(library):  # noqa: F811
    window = (datetime(2001, 1, 1, tzinfo=timezone.utc), datetime(2001, 2, 1, tzinfo=timezone.utc))
    with SessionLocal() as db:
        vs = db.scalars(select(KbVersion).join(KbItem, KbVersion.id == KbItem.current_version_id).where(
            KbItem.retired.is_(False), KbItem.doc_type == "SRC", KbVersion.title.like("%示范引用法%"))).all()[:2]
        assert len(vs) == 2
        saved = [(v.published_at, v.auto_review) for v in vs]
        vs[0].published_at, vs[0].auto_review = datetime(2001, 1, 5, tzinfo=timezone.utc), "platform"
        vs[1].published_at = datetime(2001, 1, 6, tzinfo=timezone.utc)
        db.commit()
        audit.record(db, actor="t", action="kb.eval_compare", result="allowed", passed=False, questions=10,
                     model="m", no_kb_accuracy=0.5, with_kb_accuracy=0.85, no_kb_fabricated=3, with_kb_fabricated=0)
        db.commit()
        try:
            m = _by_key(indicators.compute(db, *window, project_ids=[]))
        finally:
            for v, (at, ar) in zip(vs, saved):
                v.published_at, v.auto_review = at, ar
            # 补一条达标记录：最近一次对比评测未达标会暂停自动发布（compare.gate_reason），影响后面的用例
            audit.record(db, actor="t", action="kb.eval_compare", result="allowed", passed=True, questions=10,
                         model="m", no_kb_accuracy=0.5, with_kb_accuracy=0.95, no_kb_fabricated=0,
                         with_kb_fabricated=0)
            db.commit()
    assert m["auto_publish_rate"]["value"] == 0.5 and m["auto_publish_rate"]["detail"]["published"] == 2
    assert m["human_minutes_per_100"]["value"] is None
    assert m["human_minutes_per_100"]["detail"]["human_reviews_per_100"] == 200.0   # 每条人工两审（内容 + 合规）
    assert m["eval_accuracy"]["value"] == 0.85 and m["eval_accuracy"]["status"] == "breach"
    assert m["gate_first_pass"]["value"] is None and m["gate_first_pass"]["status"] == "na"   # 没有签署


def test_api_only_for_project_lead():
    lead = create_user(("consultant", "project_lead"), {})
    mgr = create_user(("consultant",), {})
    with logged_in_client(app, lead) as c:
        r = c.get("/api/indicators?days=30")
        assert r.status_code == 200
        body = r.json()
        assert set(_by_key(body)) == KEYS and body["days"] == 30
        assert c.get("/api/indicators?days=0").status_code == 422
    with logged_in_client(app, mgr) as c:
        assert c.get("/api/indicators").status_code == 403
