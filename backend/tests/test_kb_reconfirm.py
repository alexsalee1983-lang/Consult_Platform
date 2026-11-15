"""复审到期由知识审核 Agent 复查（人工环节复查 K7）：通过的顺延复审日期并按比例抽检，存疑的留给知识专家。"""
import json
from datetime import date, timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import agent_review, meta, reconfirm_agent
from app.kb import review as rv
from app.models import AuditLog, KbItem, KbVersion

from .test_kb_agent_review import BODY, PASS, _submitted, lib, model  # noqa: F401  复用夹具

YESTERDAY = date.today() - timedelta(days=1)


def _published_due(db, author, law) -> KbVersion:
    v = _submitted(db, author, law, BODY)
    assert agent_review.run(db, v).status == "published"
    v.review_date, v.sample_state = YESTERDAY, ""
    db.commit()
    return v


def _record(v: KbVersion):
    with SessionLocal() as db:
        return next(r for r in db.get(KbVersion, v.id).reviews if r.review_role == "reconfirm")


def test_pass_extends_and_samples(lib, model, monkeypatch):  # noqa: F811
    (author, *_), law = lib
    model(PASS)
    monkeypatch.setattr(get_settings(), "kb_sample_rate_agent", 0.0)
    with SessionLocal() as db:
        v = _published_due(db, db.merge(author), law)
        monkeypatch.setattr(get_settings(), "kb_sample_rate_agent", 1.0)
        assert v.id in reconfirm_agent.scan_due(db)
        db.refresh(v)
        assert _record(v).decision == "queued"
        assert v.id not in reconfirm_agent.scan_due(db)                     # 重复扫描不重复排
        assert reconfirm_agent.run(db, v) == "extended"
        assert v.review_date == meta.default_review_date(v.item.layer, date.today()) and v.sample_state == "pending"
        r = _record(v)
        assert r.decision == "approve" and r.findings["conclusion"] == "通过" and v.body.endswith("一并进行。")
        assert reconfirm_agent.run(db, v) == "skipped"
        log = db.scalar(select(AuditLog).where(AuditLog.action == "kb.agent_reconfirm").order_by(AuditLog.id.desc()))
        detail = json.loads(log.detail)
        assert detail["outcome"] == "extended" and detail["prompt_version"] and "风险评估" not in log.detail


def test_flag_stays_for_experts(lib, model):  # noqa: F811
    (author, *_), law = lib
    model(PASS)
    with SessionLocal() as db:
        v = _published_due(db, db.merge(author), law)
    model({**PASS, "issues": [{"severity": "重要", "location": "第二条", "problem": "依据已修订", "suggestion": "核对新版"}],
           "conclusion": "存疑"})
    with SessionLocal() as db:
        v = db.get(KbVersion, v.id)
        reconfirm_agent.scan_due(db)
        assert reconfirm_agent.run(db, v) == "flagged"
        assert v.review_date == YESTERDAY and _record(v).decision == "flag"
        assert v.id not in reconfirm_agent.scan_due(db)                     # 本周期查过，留给人
        assert v.id in {x.id for x in rv.review_due(db, 0)}
        assert "依据已修订" in _record(v).findings["issues"][0]["problem"]


def test_out_of_scope(lib, model):  # noqa: F811
    (author, *_), law = lib
    model(PASS)
    with SessionLocal() as db:
        v = _published_due(db, db.merge(author), law)
        v.single_review = True                                              # 待补复审的要人补
        db.commit()
        src = db.scalars(select(KbItem).where(KbItem.doc_type == "SRC", KbItem.retired.is_(False))).first()
        cur = db.get(KbVersion, src.current_version_id)
        cur.review_date = YESTERDAY                                          # 依据原文照旧人工复审
        db.commit()
        ids = {x.id for x in reconfirm_agent.due(db)}
        assert v.id not in ids and cur.id not in ids
        assert reconfirm_agent.run(db, v) == "skipped"
