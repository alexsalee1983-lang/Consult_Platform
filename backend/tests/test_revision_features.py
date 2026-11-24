"""四期 F4②：改稿与驳回特征表 revision_features（迁移 0054）。

改稿挖掘跑完写改稿特征（按交付件 + 来源先删后写，幂等）；项目经理驳回评审阻断问题时写一条评审驳回特征；
知识区公司做法确认驳回只读汇总（分库，不跨库写）；特征表不存正文，只存计数、改动幅度和摘录哈希；
GET /api/revision-features/summary 只给项目负责人、知识管理员，按项目过滤时非成员 404，开关关或生产 404。"""
import secrets

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import (AuditLog, Deliverable, DeliverableSection, KbItem, KbPracticeDecision, KbUpload, KbVersion,
                        Project, ProjectMember, QualityReview, RevisionFeature, User)
from app.project import quality
from app.services import storage

from .conftest import create_user, logged_in_client
from .test_revision_mining import _project, _url

URL = "/api/revision-features/summary"
WORDS = ("SIEM", "夜间", "白班", "纸质", "驳回理由")


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setattr(get_settings(), "f4_revision_mining", True)
    monkeypatch.setattr(storage, "enabled", lambda: False)


def _rows(code: str, source: str | None = None) -> list[RevisionFeature]:
    with SessionLocal() as db:
        pid = db.scalar(select(Project.id).where(Project.code == code))
        stmt = select(RevisionFeature).where(RevisionFeature.project_id == pid)
        if source:
            stmt = stmt.where(RevisionFeature.source == source)
        rows = list(db.scalars(stmt.order_by(RevisionFeature.kind)))
        db.expunge_all()
        return rows


def _no_text(rows: list[RevisionFeature]) -> None:
    for r in rows:
        values = " ".join(str(getattr(r, c.key)) for c in RevisionFeature.__table__.columns)
        assert not any(w in values for w in WORDS), values
        assert len(r.text_hash) == 16


def _mine(code: str) -> None:
    with logged_in_client(app, create_user(("consultant",), {code: "member"})) as c:
        assert c.post(_url(code)).status_code == 200


def test_mine_writes_revision_features_without_text(on):
    code = _project()
    _mine(code)
    rows = _rows(code, "revision")
    assert [(r.kind, r.count, r.section_key) for r in rows] == [("add", 1, "D4.1"), ("delete", 1, "D4.1"),
                                                                 ("rewrite", 1, "D4.1")]
    rw = next(r for r in rows if r.kind == "rewrite")
    assert 0 < rw.change_ratio < 0.5
    assert next(r for r in rows if r.kind == "delete").change_ratio == 1.0
    _no_text(rows)
    with SessionLocal() as db:
        log = db.scalar(select(AuditLog).where(AuditLog.action == "f4.revision_mining", AuditLog.project_code == code)
                        .order_by(AuditLog.id.desc()))
        assert '"features": 3' in log.detail and "SIEM" not in log.detail


def test_mine_again_is_idempotent(on):
    code = _project()
    _mine(code)
    first = [(r.kind, r.count, r.text_hash) for r in _rows(code)]
    _mine(code)
    assert [(r.kind, r.count, r.text_hash) for r in _rows(code)] == first and len(first) == 3


def _review_project() -> tuple[str, int]:
    code = f"F4W-{secrets.token_hex(3).upper()}"
    name, _, _ = create_user(("consultant",), {})
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier="standard", flowback_allowed=True)
        d = Deliverable(code="D4", title="差距分析报告", status="review", revision=1)
        p.deliverables = [d]
        db.add(p)
        db.flush()
        u = db.scalar(select(User).where(User.username == name))
        db.add(ProjectMember(project_id=p.id, user_id=u.id, project_role="manager", added_by="u"))
        db.add(DeliverableSection(deliverable_id=d.id, seq=1, title="现状概述", current_version=0, created_by="u"))
        db.add(QualityReview(project_id=p.id, deliverable_id=d.id, revision=1, status="done", created_by="u",
                             issues=[{"severity": "阻断", "by": "agent", "location": "第 1 章", "dimension": "完整性",
                                      "problem": "SIEM 选型缺少依据", "suggestion": "补充"}]))
        db.commit()
        return code, u.id


def _waive(code: str, uid: int) -> None:
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        d = db.scalar(select(Deliverable).where(Deliverable.project_id == p.id))
        r = db.scalar(select(QualityReview).where(QualityReview.deliverable_id == d.id))
        quality.waive(db, db.get(User, uid), p, d, r, 0, "驳回理由：客户已接受该风险，书面确认在案")


def test_waiver_writes_review_waiver_feature(on):
    code, uid = _review_project()
    _waive(code, uid)
    rows = _rows(code, "review_waiver")
    assert [(r.kind, r.count, r.section_key) for r in rows] == [("waiver", 1, "D4.1")]
    _no_text(rows)


def test_waiver_feature_not_written_when_switch_off():
    code, uid = _review_project()
    _waive(code, uid)
    assert _rows(code) == []


def _practice_reject(code: str) -> None:
    with SessionLocal() as db:
        up = KbUpload(object_key=f"f4ftest/{secrets.token_hex(6)}", filename="x.md", sha256=secrets.token_hex(32),
                      uploaded_by="u", guard_categories=[], status="deid_passed", source_project=code)
        db.add(up)
        db.flush()
        item = KbItem(doc_id=f"L2-TST-F4F-PIT-{secrets.randbelow(900000) + 100000}", layer="L2", domain="TST",
                      product="F4F", doc_type="PIT", stage="通用", import_key="f4f-" + secrets.token_hex(4))
        db.add(item)
        db.flush()
        v = KbVersion(item_id=item.id, version=1, status="rejected", title="虚拟候选", body="正文",
                      source_type="project", upload_id=up.id)
        db.add(v)
        db.flush()
        db.add(KbPracticeDecision(version_id=v.id, reviewer="e", decision="reject"))
        db.add(KbPracticeDecision(version_id=v.id, reviewer="e", decision="adopt"))
        db.commit()


def test_summary_aggregates_sources_and_practice_rejects(on):
    code = _project()
    _mine(code)
    _practice_reject(code)
    other = _project()
    _mine(other)                                       # 负责人不是这个项目的成员，不计入
    with logged_in_client(app, create_user(("project_lead",), {code: "member"})) as c:
        r = c.get(URL, params={"project": code})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["project"] == code
        assert data["by_source"] == {"revision": 3, "review_waiver": 0, "practice_reject": 1}
        keys = {(x["section_key"], x["source"]): x["count"] for x in data["by_section"]}
        assert keys == {("D4.1", "revision"): 3, ("", "practice_reject"): 1}
        whole = c.get(URL).json()
        assert whole["project"] is None and whole["by_source"]["revision"] == 3
        assert c.get(URL, params={"project": other}).status_code == 404
        assert c.get(URL, params={"project": "NOPE-X"}).status_code == 404


def test_summary_permissions_and_switch(monkeypatch):
    code = _project()
    with logged_in_client(app, create_user(("consultant",), {code: "manager"})) as c:
        assert c.get(URL).status_code == 403
    with logged_in_client(app, create_user(("project_lead",), {code: "member"})) as c:
        assert c.get(URL).status_code == 404                       # 开关默认关
        monkeypatch.setattr(get_settings(), "f4_revision_mining", True)
        assert c.get(URL).status_code == 200
        monkeypatch.setattr(get_settings(), "app_env", "prod")
        assert c.get(URL).status_code == 404
    monkeypatch.setattr(get_settings(), "app_env", "dev")
    with logged_in_client(app, create_user(("knowledge_admin",), {})) as c:
        r = c.get(URL)
        assert r.status_code == 200 and r.json()["by_source"]["revision"] == 0
