"""四期 F4③：经验萃取触发引擎（3 类信号、先查后问三道过滤、打分、每人每周 ≤ 3 题、超时与跳过）。

信号跨项目按主题聚合，问题表不存正文和项目代号；严格档、不同意回流的项目不出信号；开关关或生产不出题、接口 404；
专家回答过机密级检测后投料（进 B17），别人的题 404、已收尾 409；时间比较不随机器时区变（SQLite 读回无时区）。"""
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import (AuditLog, Confirmation, Deliverable, ExtractionQuestion, KbItem, KbUpload, KbVersion, Project,
                        RevisionFeature, User)
from app.project import extraction_trigger as et
from app.services import storage

from .conftest import create_user, logged_in_client

NOW = datetime(2026, 12, 2, 2, 0, tzinfo=timezone.utc)   # 周三


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setattr(get_settings(), "f4_revision_mining", True)
    with SessionLocal() as db:
        db.execute(delete(ExtractionQuestion))
        db.commit()


def _project(tier: str = "standard", flowback: bool = True) -> tuple[int, int]:
    with SessionLocal() as db:
        p = Project(code=f"ET-{secrets.token_hex(3).upper()}", service="SOC 运营体系设计", industry="银行", tier=tier,
                    flowback_allowed=flowback)
        d = Deliverable(code="D4", title="差距分析报告", status="signed")
        p.deliverables = [d]
        db.add(p)
        db.commit()
        return p.id, d.id


def _gap_card(pid: int, slots: list[str]) -> None:
    with SessionLocal() as db:
        db.add(Confirmation(project_id=pid, kind="知识不足", title=f"第 3 步差距分析缺少必需知识：{'、'.join(slots)}",
                            agent="差距分析 Agent"))
        db.commit()


def _feature(pid: int, did: int, key: str, source: str = "revision", kind: str = "rewrite", count: int = 2) -> None:
    with SessionLocal() as db:
        db.add(RevisionFeature(project_id=pid, deliverable_id=did, section_key=key, source=source, kind=kind,
                               count=count, change_ratio=0.5, text_hash="0" * 16))
        db.commit()


def _questions(topic: str) -> list[ExtractionQuestion]:
    with SessionLocal() as db:
        rows = list(db.scalars(select(ExtractionQuestion).where(ExtractionQuestion.topic == topic)
                               .order_by(ExtractionQuestion.id)))
        db.expunge_all()
        return rows


def _run(now: datetime = NOW) -> dict:
    with SessionLocal() as db:
        return et.run_week(db, now)


def _uid(username: str) -> int:
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.username == username))


def _topic() -> str:
    return f"t-{secrets.token_hex(3)}"


def test_three_signals_filters_and_scoring(on, monkeypatch):
    monkeypatch.setattr(et, "_experts", lambda db: [])
    gap, low, rev, solo, waiver = _topic(), _topic(), "Z9.1", "Z9.2", "Z9.3"
    a, b, c = _project(), _project(), _project()
    for pid, _ in (a, b, c):
        _gap_card(pid, [gap])
    _gap_card(a[0], [low])
    for pid, did in (a, b):
        _feature(pid, did, rev, kind="delete", count=2)
        _feature(pid, did, rev, kind="add", count=9)      # 新增不算“删改”
    _feature(a[0], a[1], solo, count=9)                  # 只一个项目，不成簇
    for pid, did in (a, b):
        _feature(pid, did, waiver, source="review_waiver", kind="waiver", count=1)
    r = _run()
    assert r["new"] >= 3 and r["low"] >= 1
    q = _questions(gap)
    assert [(x.signal, x.projects, x.count, x.score, x.status) for x in q] == [("knowledge_gap", 3, 3, 0.8, "pending")]
    assert [(x.status, x.filter_reason) for x in _questions(low)] == [("filtered", "low")]
    assert [(x.signal, x.projects, x.count, x.status) for x in _questions(rev)] == [("revision_cluster", 2, 4, "pending")]
    assert _questions(solo) == []
    assert [(x.signal, x.count) for x in _questions(waiver)] == [("review_waiver", 2)]
    # 近期问过：再跑一轮不落新行
    r2 = _run(NOW + timedelta(days=1))
    assert r2["recent"] >= 4 and len(_questions(gap)) == 1
    # 超过 RECENT_DAYS 后可以再问
    _run(NOW + timedelta(days=et.RECENT_DAYS + 1))
    assert len(_questions(gap)) == 2
    with SessionLocal() as db:
        log = db.scalar(select(AuditLog).where(AuditLog.action == "f4.extraction.run").order_by(AuditLog.id.desc()))
        assert gap not in log.detail and "ET-" not in log.detail


def test_known_in_kb_is_filtered(on, monkeypatch):
    monkeypatch.setattr(et, "_experts", lambda db: [])
    slot = _topic()
    with SessionLocal() as db:
        item = KbItem(doc_id=f"KB-ET{secrets.token_hex(4)}", layer="L3", domain="OPS", product="SOC", doc_type="MTD",
                      stage="通用", knowledge_kind="practice", slot=slot)
        db.add(item)
        db.flush()
        v = KbVersion(item_id=item.id, version=1, status="published", title="已有做法", body="x", source_type="expert")
        db.add(v)
        db.flush()
        item.current_version_id = v.id
        db.commit()
    for pid, _ in (_project(), _project(), _project()):
        _gap_card(pid, [slot])
    _run()
    assert [(x.status, x.filter_reason) for x in _questions(slot)] == [("filtered", "known")]


def test_strict_noflowback_and_switch_off(on, monkeypatch):
    monkeypatch.setattr(et, "_experts", lambda db: [])
    t1, t2 = _topic(), _topic()
    for _ in range(3):
        _gap_card(_project(tier="strict")[0], [t1])
        _gap_card(_project(flowback=False)[0], [t2])
    _run()
    assert _questions(t1) == [] and _questions(t2) == []
    monkeypatch.setattr(get_settings(), "f4_revision_mining", False)
    t3 = _topic()
    for _ in range(3):
        _gap_card(_project()[0], [t3])
    assert _run()["signals"] == 0 and _questions(t3) == []


def test_weekly_cap_and_timeout(on, monkeypatch):
    user = create_user(("knowledge_expert",))
    uid = _uid(user[0])
    monkeypatch.setattr(et, "_experts", lambda db: [uid])
    topics = [_topic() for _ in range(5)]
    for t in topics:
        for _ in range(3):
            _gap_card(_project()[0], [t])
    _run()
    with SessionLocal() as db:
        asked = list(db.scalars(select(ExtractionQuestion).where(ExtractionQuestion.assignee_id == uid)))
        pending = db.scalar(select(ExtractionQuestion.id).where(ExtractionQuestion.status == "pending"))
    assert len(asked) == et.WEEKLY_CAP and pending is not None
    # 同一周再跑不多派；超时后收尾为跳过（timeout），下一周继续派
    _run(NOW + timedelta(days=2))
    with SessionLocal() as db:
        assert len(list(db.scalars(select(ExtractionQuestion).where(ExtractionQuestion.assignee_id == uid)))) == 3
    _run(NOW + timedelta(days=et.ANSWER_DAYS))
    with SessionLocal() as db:
        rows = list(db.scalars(select(ExtractionQuestion).where(ExtractionQuestion.assignee_id == uid)))
        assert sum(1 for q in rows if q.skip_reason == "timeout") == 3
        assert sum(1 for q in rows if q.status == "asked") == et.WEEKLY_CAP   # 新的一周再派满
        st = et.stats(db)
    assert st["skipped"] == 3 and st["skip_rate"] == 1.0


def test_naive_times_compare_in_any_timezone(on, monkeypatch):
    """SQLite 读回无时区；在东八区机器上也按 UTC 比较（#418 同类问题）。"""
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    user = create_user(("knowledge_expert",))
    uid = _uid(user[0])
    monkeypatch.setattr(et, "_experts", lambda db: [uid])
    with SessionLocal() as db:
        db.add(ExtractionQuestion(signal="knowledge_gap", topic=_topic(), projects=3, count=3, score=0.8,
                                  status="asked", assignee_id=uid, asked_at=NOW.replace(tzinfo=None),
                                  due_at=(NOW + timedelta(hours=1)).replace(tzinfo=None)))
        db.commit()
    assert _run(NOW + timedelta(minutes=59))["expired"] == 0
    assert _run(NOW + timedelta(minutes=61))["expired"] == 1


def _asked(uid: int) -> int:
    with SessionLocal() as db:
        q = ExtractionQuestion(signal="revision_cluster", topic="D4.1", projects=2, count=4, score=0.73,
                               status="asked", assignee_id=uid, asked_at=NOW, due_at=NOW + timedelta(days=7))
        db.add(q)
        db.commit()
        return q.id


def test_api_answer_skip_and_isolation(on, monkeypatch):
    stored: dict = {}
    monkeypatch.setattr(storage, "enabled", lambda: True)
    monkeypatch.setattr(storage, "put", lambda area, key, data, ct: stored.__setitem__(key, data))
    expert, other = create_user(("knowledge_expert",)), create_user(("knowledge_expert",))
    q1, q2, q3 = _asked(_uid(expert[0])), _asked(_uid(expert[0])), _asked(_uid(other[0]))
    with logged_in_client(app, expert) as c:
        r = c.get("/api/extraction/questions")
        assert r.status_code == 200
        ids = [x["id"] for x in r.json()["questions"]]
        assert q1 in ids and q2 in ids and q3 not in ids
        assert "D4.1" in r.json()["questions"][0]["question"]
        assert c.post(f"/api/extraction/questions/{q3}/skip").status_code == 404     # 别人的题
        assert c.post(f"/api/extraction/questions/{q1}/answer",
                      json={"body": "管理口令 admin/P@ssw0rd123 写在交接本上"}).status_code == 422
        r = c.post(f"/api/extraction/questions/{q1}/answer", json={"body": "交接班应写明未闭环告警的处置责任人。"})
        assert r.status_code == 200, r.text
        up_id = r.json()["upload_id"]
        assert c.post(f"/api/extraction/questions/{q1}/answer", json={"body": "再答一次"}).status_code == 409
        assert c.post(f"/api/extraction/questions/{q2}/skip").json()["status"] == "skipped"
        assert c.get("/api/extraction/stats").json()["answered"] >= 1
    with SessionLocal() as db:
        up = db.get(KbUpload, up_id)
        assert up.source_project == "" and up.filename.startswith("经验萃取-")
        q = db.get(ExtractionQuestion, q1)
        assert q.status == "answered" and q.upload_id == up_id
        logs = [a.detail for a in db.scalars(select(AuditLog).where(AuditLog.action == "f4.extraction.answer"))]
        assert logs and not any("交接班" in d or "P@ss" in d for d in logs)
    with logged_in_client(app, create_user(("consultant",))) as c:
        assert c.get("/api/extraction/questions").status_code == 403


def test_api_404_when_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "f4_revision_mining", False)
    with logged_in_client(app, create_user(("knowledge_expert",))) as c:
        assert c.get("/api/extraction/questions").status_code == 404
        assert c.get("/api/extraction/stats").status_code == 404


def test_cli_extraction_week(on, capsys):
    from app import cli

    assert cli.main(["extraction-week"]) == 0
    assert "经验萃取：" in capsys.readouterr().out
