"""三期 B17（拆出）：公司做法确认队列（知识区迁移 k0018）。

- 队列只列待审的公司做法候选，按章节就绪度从低到高排（同槽位已发布的公司做法、黄金样例越少越靠前；无槽位排最后）。
- 卡片写出处、依据（知识审核 Agent 意见）、把握程度、还缺什么。
- 采纳 / 修改 / 驳回，可批量；每条记录耗时与修改幅度（kb_practice_decisions，不记正文）。
- 只有知识专家能拍板，作者不能确认自己的条目；非公司做法不进这个队列。
"""
import uuid

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.kb import practice_queue as pq
from app.kb import review as rv
from app.kb.items import ItemMeta, VersionContent, create_item
from app.main import app
from app.models import AuditLog, KbPracticeDecision, KbReview, KbVersion, User

from .conftest import create_user, logged_in_client

BODY = "## 做法\n\n按示范步骤开展值班交接。\n\n## 适用\n\n中型 SOC。"


def _user(*roles: str) -> tuple[User, tuple]:
    creds = create_user(roles)
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == creds[0])), creds


def _candidate(author: User, slot: str = "", doc_type: str = "TPL", source: str = "expert",
               agent: dict | None = None) -> int:
    with SessionLocal() as db:
        author = db.merge(author)
        m = ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type=doc_type, stage="设计", capabilities=["运营"],
                     slot=slot)
        c = VersionContent(title=f"示范做法{uuid.uuid4().hex[:6]}", body=BODY, source_type=source,
                           source_detail="专家萃取：示范" if source != "public" else "公开资料：示范")
        v = create_item(db, m, c, created_by=author.username)
        v.deid_status = "passed" if source != "public" else "not_needed"   # 模拟投料去标识已确认（只在测试里直接改）
        db.commit()
        rv.submit(db, author, v)
        db.add(KbReview(version_id=v.id, reviewer="agent:kb-reviewer", review_role="agent", decision="approve",
                        comment="依据：专家萃取访谈记录", findings=agent or {}))
        db.commit()
        return v.id


def test_queue_sorted_by_slot_readiness_and_card_fields():
    author, _ = _user("knowledge_expert")
    expert, _ = _user("knowledge_expert")
    slot_ready, slot_empty = f"SOC.D5.r{uuid.uuid4().hex[:4]}", f"SOC.D5.e{uuid.uuid4().hex[:4]}"
    done = _candidate(author, slot_ready)
    with SessionLocal() as db:
        rv.review(db, db.merge(expert), db.get(KbVersion, done), "content", "approve")
    a = _candidate(author, slot_ready, agent={"confidence": "中", "issues": ["缺适用规模"]})
    b = _candidate(author, slot_empty)
    c = _candidate(author, "")
    with SessionLocal() as db:
        cards = [x for x in pq.queue(db) if x["version_id"] in (a, b, c)]
    assert [x["version_id"] for x in cards] == [b, a, c]
    card = next(x for x in cards if x["version_id"] == a)
    assert card["slot_ready"] == 1 and card["source_detail"] == "专家萃取：示范"
    assert card["basis"] == "依据：专家萃取访谈记录" and card["confidence"] == "中" and card["missing"] == ["缺适用规模"]


def test_non_practice_not_in_queue():
    author, _ = _user("knowledge_expert")
    vid = _candidate(author, doc_type="REF")
    with SessionLocal() as db:
        assert vid not in [x["version_id"] for x in pq.queue(db)]
        with pytest.raises(rv.ReviewError) as e:
            pq.decide(db, db.merge(_user("knowledge_expert")[0]), vid, "adopt")
        assert e.value.status == 409


def test_adopt_modify_reject_record_time_and_change():
    author, _ = _user("knowledge_expert")
    expert, _ = _user("knowledge_expert")
    ids = [_candidate(author) for _ in range(3)]
    new_body = BODY + "\n\n## 补充\n\n交接须双人签字。"
    with SessionLocal() as db:
        ex = db.merge(expert)
        pq.decide(db, ex, ids[0], "adopt", elapsed_ms=12000)
        pq.decide(db, ex, ids[1], "modify", body=new_body, elapsed_ms=95000)
        pq.decide(db, ex, ids[2], "reject", comment="与现行做法重复", elapsed_ms=5000)
        vs = [db.get(KbVersion, i) for i in ids]
        assert [v.status for v in vs] == ["published", "published", "rejected"]
        assert vs[1].body == new_body
        rows = {r.version_id: r for r in db.scalars(select(KbPracticeDecision).where(KbPracticeDecision.version_id.in_(ids)))}
        assert rows[ids[0]].decision == "adopt" and rows[ids[0]].change_ratio == 0 and rows[ids[0]].elapsed_ms == 12000
        assert 0 < rows[ids[1]].change_ratio < 1 and rows[ids[1]].elapsed_ms == 95000
        assert rows[ids[2]].decision == "reject"
        logs = db.scalars(select(AuditLog).where(AuditLog.action == "kb.practice_decide")).all()
        assert logs and all("交接须双人签字" not in str(x.detail) for x in logs)   # 审计不记正文
        s = pq.stats(db)
        assert s["decided"] >= 3 and set(s["by_decision"]) >= {"adopt", "modify", "reject"}


def test_author_cannot_confirm_own_and_modify_needs_body():
    author, _ = _user("knowledge_expert")
    expert, _ = _user("knowledge_expert")
    vid = _candidate(author)
    with SessionLocal() as db:
        with pytest.raises(rv.ReviewError) as e:
            pq.decide(db, db.merge(author), vid, "adopt")
        assert e.value.status == 403
        with pytest.raises(rv.ReviewError) as e:
            pq.decide(db, db.merge(expert), vid, "modify", body="")
        assert e.value.status == 422
        with pytest.raises(rv.ReviewError) as e:              # 修改后的正文同样过机密级检测
            pq.decide(db, db.merge(expert), vid, "modify", body=BODY + "\n口令：P@ssw0rd!2026")
        assert e.value.status == 422
        assert db.get(KbVersion, vid).status == "in_review"
        assert db.scalar(select(KbPracticeDecision).where(KbPracticeDecision.version_id == vid)) is None


def test_api_batch_and_permissions():
    author, _ = _user("knowledge_expert")
    _, expert_creds = _user("knowledge_expert")
    _, admin_creds = _user("knowledge_admin")
    _, consultant_creds = _user("consultant")
    ids = [_candidate(author) for _ in range(2)]
    ex = logged_in_client(app, expert_creds)
    adm = logged_in_client(app, admin_creds)
    con = logged_in_client(app, consultant_creds)
    try:
        q = ex.get("/api/kb/practice-queue")
        assert q.status_code == 200 and set(ids) <= {x["version_id"] for x in q.json()}
        assert adm.get("/api/kb/practice-queue").status_code == 200
        assert con.get("/api/kb/practice-queue").status_code == 403
        items = [{"version_id": ids[0], "decision": "adopt", "elapsed_ms": 3000},
                 {"version_id": ids[1], "decision": "reject", "comment": "不适用"},
                 {"version_id": 999999, "decision": "adopt"}]
        r = adm.post("/api/kb/practice-queue/decide", json={"items": items[:1]})   # 知识管理员不能拍板
        assert r.status_code == 200 and r.json()["results"][0]["ok"] is False
        r = ex.post("/api/kb/practice-queue/decide", json={"items": items})
        assert r.status_code == 200
        res = r.json()["results"]
        assert [x["ok"] for x in res] == [True, True, False] and res[2]["status"] == 404
        assert [x["version_status"] for x in res[:2]] == ["published", "rejected"]
        assert ex.get("/api/kb/practice-queue/stats").status_code == 200
        assert con.get("/api/kb/practice-queue/stats").status_code == 403
        assert ex.post("/api/kb/practice-queue/decide", json={"items": []}).status_code == 422
    finally:
        for c in (ex, adm, con):
            c.__exit__(None, None, None)
