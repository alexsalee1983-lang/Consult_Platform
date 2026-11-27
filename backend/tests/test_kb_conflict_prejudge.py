"""知识审核“不重复、不冲突”规则预判与积压重跑（四期试用缺陷，负责人 2026-11-25 同意）。"""
import argparse
import json
import uuid

import pytest
from sqlalchemy import select

from app import cli
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import agent_review, conflict_rules
from app.kb import review as rv
from app.models import AuditLog, Job, KbReview, KbVersion

from . import test_kb_agent_review as base

BODY, PASS, _submitted = base.BODY, base.PASS, base._submitted
lib, model = base.lib, base.model              # 复用 B11 用例的示范法与模拟模型

UNSURE = {**PASS, "checks": {**PASS["checks"], "no_conflict": {"pass": False, "reason": "存疑：与其他法律条文重复或冲突"}},
          "issues": [{"severity": "重要", "check": "no_conflict", "location": "全文", "problem": "可能与其他条文重复",
                      "suggestion": "核对"}],
          "conclusion": "存疑"}
OTHER = "## 第一条 立法目的\n\n第一条说明示范审核法的立法目的，是为了保障示范审核安全。"


_MADE: list[int] = []


@pytest.fixture()
def no_gate(monkeypatch):
    """不查引用；用例里发布的已有条目用完下线，免得在 PostgreSQL 上成为其他用例的相近条目。"""
    monkeypatch.setattr(get_settings(), "kb_citation_gate", False)
    yield
    with SessionLocal() as db:
        for vid in _MADE:
            db.get(KbVersion, vid).item.retired = True
        db.commit()
    _MADE.clear()


def _published(db, people, law: str, body: str, **extra) -> KbVersion:
    """人工两项审核发布一条已有条目（作为相近条目）。"""
    author, expert, admin = (db.merge(p) for p in people)
    v = _submitted(db, author, law, body, **extra)
    rv.review(db, expert, v, "content", "approve")
    rv.review(db, admin, v, "compliance", "approve")
    assert v.status == "published"
    _MADE.append(v.id)
    return v


def _similar(monkeypatch, *versions: KbVersion) -> None:
    monkeypatch.setattr(rv, "similar_items", lambda db, v, limit=5: [
        {"doc_id": x.item.doc_id, "title": x.title, "score": 1.0} for x in versions])


def _agent_review(v: KbVersion) -> KbReview:
    return max((x for x in v.reviews if x.review_role == "agent"), key=lambda x: x.id)


def test_normalize_helpers():
    a = KbVersion(ref_code="GB/T 22239-2019", ref_document="", ref_version="")
    b = KbVersion(ref_code="GB/T22239—2008", ref_document="", ref_version="")
    assert conflict_rules.doc_keys(a) == conflict_rules.doc_keys(b)
    assert conflict_rules.version_year(a) == 2019 and conflict_rules.version_year(b) == 2008
    c = KbVersion(ref_document="中华人民共和国网络安全法（2025年修正）", ref_code="", ref_version="")
    d = KbVersion(ref_document="《网络安全法》", ref_code="", ref_version="")
    assert conflict_rules.doc_keys(c) == conflict_rules.doc_keys(d)
    assert conflict_rules.body_hash("## 第二条\n运营者，应当 评估。") == conflict_rules.body_hash("第二条 运营者应当评估")


def test_same_document_other_clause_not_conflict(lib, model, monkeypatch, no_gate):
    people, law = lib
    m = model(UNSURE)
    with SessionLocal() as db:
        old = _published(db, people, law, OTHER, title=f"{law}第一条解读")
        _similar(monkeypatch, old)
        v = _submitted(db, db.merge(people[0]), law, BODY)
        out = agent_review.run(db, v)
        assert out.status == "published", _agent_review(v).findings
        f = _agent_review(v).findings
        assert f["checks"]["no_conflict"]["pass"] and f["checks"]["no_conflict"]["rule"]
        assert f["prejudge"]["relations"] == [{"doc_id": old.item.doc_id, "kind": "different_clause"}]
        assert f["issues"][0]["severity"] == "建议" and f["issues"][0]["original_severity"] == "重要"
        assert "已由规则判定" in m.requests[0]["messages"][-1]["content"]


def test_duplicate_rejected_without_model(lib, model, monkeypatch, no_gate):
    people, law = lib
    m = model(PASS)
    with SessionLocal() as db:
        old = _published(db, people, law, BODY, title=f"{law}第二条解读甲")
        _similar(monkeypatch, old)
        v = _submitted(db, db.merge(people[0]), law, BODY.replace("，", ",  "))
        out = agent_review.run(db, v)
        assert out.status == "rejected" and v.status == "rejected" and not m.requests
        r = _agent_review(v)
        assert r.decision == "reject" and old.item.doc_id in r.comment
        assert old.item.doc_id in r.findings["checks"]["no_conflict"]["reason"]
        log = db.scalar(select(AuditLog).where(AuditLog.action == "kb.agent_review").order_by(AuditLog.id.desc()))
        detail = json.loads(log.detail)
        assert detail["outcome"] == "duplicate" and detail["prejudge"] == {"duplicate": 1}
        assert "风险评估" not in log.detail


def test_newer_version_wins_old_marked_recheck(lib, model, monkeypatch, no_gate):
    people, law = lib
    model(UNSURE)
    with SessionLocal() as db:
        old = _published(db, people, law, BODY + "（旧版）", ref_version="2027")
        _similar(monkeypatch, old)
        v = _submitted(db, db.merge(people[0]), law, BODY)                    # ref_version 2029
        assert agent_review.run(db, v).status == "published"
        assert _agent_review(v).findings["prejudge"]["supersedes"] == [old.item.doc_id]
        db.refresh(old)
        assert old.recheck_state == "pending" and v.item.doc_id in old.recheck_reason


def test_older_candidate_goes_to_conflict_card(lib, model, monkeypatch, no_gate):
    people, law = lib
    model(PASS)
    with SessionLocal() as db:
        new = _published(db, people, law, BODY + "（新版）")                  # ref_version 2029
        _similar(monkeypatch, new)
        v = _submitted(db, db.merge(people[0]), law, BODY, ref_version="2027")
        assert agent_review.run(db, v).status == "manual" and v.status == "in_review"
        c = _agent_review(v).findings["checks"]["no_conflict"]
        assert not c["pass"] and c["rule"] and new.item.doc_id in c["reason"]
        assert new.recheck_state == ""


def test_jurisdiction_differs_not_conflict(lib, model, monkeypatch, no_gate):
    people, law = lib
    model(UNSURE)
    with SessionLocal() as db:
        old = _published(db, people, f"另一部示范条例{uuid.uuid4().hex[:4]}", OTHER, industries="[银行]")
        _similar(monkeypatch, old)
        v = _submitted(db, db.merge(people[0]), law, BODY, industries="[电力]")
        assert agent_review.run(db, v).status == "published"
        assert _agent_review(v).findings["prejudge"]["relations"][0]["kind"] == "jurisdiction"


def test_unresolved_sent_to_agent_with_summary(lib, model, monkeypatch, no_gate):
    people, law = lib
    m = model(UNSURE)
    with SessionLocal() as db:
        other_law = f"另一部示范条例{uuid.uuid4().hex[:4]}"
        old = _published(db, people, other_law, OTHER, summary="另一部条例的立法目的摘要")
        _similar(monkeypatch, old)
        v = _submitted(db, db.merge(people[0]), law, BODY)
        assert agent_review.run(db, v).status == "manual"
        sent = m.requests[0]["messages"][-1]["content"]
        assert old.item.doc_id in sent and "另一部条例的立法目的摘要" in sent and other_law in sent
        f = _agent_review(v).findings
        assert f["prejudge"]["verdict"] == "agent" and not f["checks"]["no_conflict"]["pass"]
        assert "rule" not in f["checks"]["no_conflict"]


def test_rerun_backlog(lib, model, monkeypatch, no_gate, capsys):
    people, law = lib
    monkeypatch.setattr(get_settings(), "kb_agent_auto_publish", False)
    with SessionLocal() as db:
        old = _published(db, people, law, OTHER, title=f"{law}第一条解读")
        v = _submitted(db, db.merge(people[0]), law, BODY)
        db.add(KbReview(version_id=v.id, reviewer=agent_review.REVIEWER, review_role="agent", decision="flag",
                        comment="存疑", findings={"checks": {"no_conflict": {"pass": False, "reason": "存疑：重复"}},
                                                  "issues": [], "conclusion": "存疑"}))
        db.commit()
        vid = v.id
    _similar(monkeypatch, old)
    monkeypatch.setattr(cli, "SessionLocal", SessionLocal)
    assert cli.kb_agent_review_rerun(argparse.Namespace(limit=None, dry_run=True)) == 0
    out = capsys.readouterr().out
    assert "pass" in out and "different_clause" in out
    with SessionLocal() as db:
        jobs = [json.loads(j.payload) for j in db.scalars(select(Job).where(Job.kind == "kb.agent_review"))]
        assert {"version_id": vid, "rerun": True} not in jobs                    # 只打印计数，不排队
    assert cli.kb_agent_review_rerun(argparse.Namespace(limit=None, dry_run=False)) == 0
    with SessionLocal() as db:
        jobs = [json.loads(j.payload) for j in db.scalars(select(Job).where(Job.kind == "kb.agent_review"))]
        assert {"version_id": vid, "rerun": True} in jobs
        v = db.get(KbVersion, vid)
        assert agent_review.run(db, v).status == "skipped"                      # 不带 rerun 仍视为已审过
    monkeypatch.setattr(get_settings(), "kb_agent_auto_publish", True)
    model(UNSURE)
    with SessionLocal() as db:
        v = db.get(KbVersion, vid)
        assert agent_review.run(db, v, rerun=True).status == "published"
        assert agent_review.run(db, v, rerun=True).status == "skipped"         # 只重跑一次
