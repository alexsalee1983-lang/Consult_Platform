"""四期 A45：知识待审积压补跑自动审核（kb-review-backfill，只用虚拟数据）。"""
import argparse
import json

from sqlalchemy import select

from app import cli
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import compliance_auto
from app.models import AuditLog, Job, KbReview, KbVersion

from . import test_kb_agent_review as ar
from .test_kb_agent_review import BODY, _submitted

OFFICIAL = "公开资料：https://law.example.gov.cn/a45/1"
lib, model = ar.lib, ar.model


def _backlog(db, author, law, **extra) -> int:
    """模拟 B43 上线前提交的积压：提交时既不交 Agent，也没有合规自动关。"""
    v = _submitted(db, db.merge(author), law, BODY, **extra)
    assert v.status == "in_review" and not v.reviews
    return v.id


def _run(dry: bool = False, limit: int | None = None) -> int:
    return cli.kb_review_backfill(argparse.Namespace(dry_run=dry, limit=limit))


def _agent_jobs(db, vid: int) -> int:
    return sum(1 for j in db.scalars(select(Job).where(Job.kind == "kb.agent_review"))
               if json.loads(j.payload).get("version_id") == vid)


def test_backfill_compliance_auto_and_exceptions(lib, model, monkeypatch, capsys):
    (author, _, _), law = lib
    monkeypatch.setattr(cli, "SessionLocal", SessionLocal)
    monkeypatch.setattr(get_settings(), "kb_agent_auto_publish", False)
    monkeypatch.setattr(get_settings(), "kb_compliance_auto", False)
    with SessionLocal() as db:
        ok = _backlog(db, author, law, source_detail=OFFICIAL)
        bad = _backlog(db, author, law, source_detail="公开资料：https://blog.example.org/a45")
    monkeypatch.setattr(get_settings(), "kb_compliance_auto", True)

    assert _run(dry=True) == 0                                        # dry-run 只打印，不写库
    assert "合规自动关" in capsys.readouterr().out
    with SessionLocal() as db:
        assert not db.get(KbVersion, ok).reviews and not db.get(KbVersion, bad).reviews

    assert _run() == 0
    with SessionLocal() as db:
        assert compliance_auto.auto_passed(db.get(KbVersion, ok))       # 合规待办消失
        b = db.get(KbVersion, bad)
        assert not compliance_auto.auto_passed(b)                       # 例外仍留给知识管理员
        assert any(r.review_role == compliance_auto.FLAG_ROLE for r in b.reviews)
        n = db.scalar(select(KbReview.id).order_by(KbReview.id.desc()))
        log = db.scalar(select(AuditLog).where(AuditLog.action == "kb.review_backfill").order_by(AuditLog.id.desc()))
        assert log is not None and "blog.example" not in log.detail and BODY[:10] not in log.detail

    assert _run() == 0                                                  # 重复跑不重复写
    with SessionLocal() as db:
        assert db.scalar(select(KbReview.id).order_by(KbReview.id.desc())) == n


def test_backfill_enqueues_agent_once(lib, model, monkeypatch):
    (author, _, _), law = lib
    monkeypatch.setattr(cli, "SessionLocal", SessionLocal)
    monkeypatch.setattr(get_settings(), "kb_agent_auto_publish", False)
    monkeypatch.setattr(get_settings(), "kb_compliance_auto", False)
    with SessionLocal() as db:
        vid = _backlog(db, author, law, source_detail=OFFICIAL)
    monkeypatch.setattr(get_settings(), "kb_agent_auto_publish", True)
    monkeypatch.setattr(get_settings(), "kb_compliance_auto", True)

    assert _run(dry=True) == 0
    with SessionLocal() as db:
        assert _agent_jobs(db, vid) == 0
    assert _run() == 0
    assert _run() == 0                                                  # 已在队列里的不重复排
    with SessionLocal() as db:
        assert _agent_jobs(db, vid) == 1
        assert not db.get(KbVersion, vid).reviews                       # 命令里不调模型，不直接判合规
