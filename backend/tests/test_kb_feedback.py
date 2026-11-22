"""知识使用反馈（三期 B29 最小版，app/kb/feedback.py）：交付件审阅与顾问对话的三键；“过时 / 不对”进待复查（缺口池，
复用 B13，就绪度不计入）；引用次数与采纳率；附言过机密级检测、严格档不收附言；审计不记附言原文；按项目删除。"""
import json
import secrets

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal, utcnow
from app.kb import feedback as fb
from app.kb import review as rv
from app.kb.items import ItemMeta, VersionContent
from app.main import app
from app.models import AuditLog, Conversation, Deliverable, KnowledgeFeedback, KbItem, KbVersion, Message, Project, User
from app.project import deliverables as dl
from app.seed import DELIVERABLES

from .conftest import ask, create_user, logged_in_client, sse


def _publish(people, title):
    with SessionLocal() as db:
        u = {k: db.scalar(select(User).where(User.username == v[0])) for k, v in people.items()}
        m = ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type="TPL", stage="设计", capabilities=["管理"])
        v = rv.new_item(db, u["author"], m, VersionContent(title=title, body=f"## {title}\n\n示范做法。",
                                                           source_type="public", source_detail="公开资料：示范"))
        rv.submit(db, u["author"], v)
        rv.review(db, u["expert"], v, "content", "approve")
        rv.review(db, u["admin"], v, "compliance", "approve")
        return v.item.doc_id


def _project(tier="standard"):
    code = f"FBK-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier=tier)
        p.deliverables = [Deliverable(code=f"D{i + 1}", title=t) for i, t in enumerate(DELIVERABLES)]
        db.add(p)
        db.commit()
    member = create_user(("consultant",), {code: "manager"})
    if tier == "strict":                               # 严格档没有客户同意不收交付件内容
        return code, member
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        u = db.scalar(select(User).where(User.username == member[0]))
        dl.set_outline(db, u, p, next(x for x in p.deliverables if x.code == "D4"), ["第1章", "第2章"])
    return code, member


def _write(code, member, seq, body, agent=""):
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        u = db.scalar(select(User).where(User.username == member[0]))
        d = next(x for x in p.deliverables if x.code == "D4")
        dl.write_section(db, u, p, d, seq, body, agent_code=agent)


def _version(doc_id):
    with SessionLocal() as db:
        item = db.scalar(select(KbItem).where(KbItem.doc_id == doc_id))
        return db.get(KbVersion, item.current_version_id)


@pytest.fixture(scope="module")
def people():
    return {"author": create_user(("knowledge_expert",)), "expert": create_user(("knowledge_expert",)),
            "admin": create_user(("knowledge_admin",))}


def _review(seq=1, version=1):
    return {"deliverable": "D4", "seq": seq, "version": version}


def test_review_three_keys_and_flag_goes_to_recheck(people):
    x, y = _publish(people, f"反馈测试-甲{secrets.token_hex(2)}"), _publish(people, "反馈测试-乙")
    code, member = _project()
    _write(code, member, 1, f"现状与建议 [{x}] [{y}]。", agent="report-writer")
    c = logged_in_client(app, member)
    try:
        r = c.get(f"/api/projects/{code}/kb-feedback", params=_review())
        assert r.status_code == 200, r.text
        assert r.json() == {"surface": "review", "doc_ids": sorted([x, y]), "mine": {}}

        r = c.post(f"/api/projects/{code}/kb-feedback", json={**_review(), "doc_id": y, "rating": "useful"})
        assert r.status_code == 200, r.text
        assert r.json()["mine"] == {y: "useful"} and r.json()["recheck_marked"] is False
        assert _version(y).recheck_state == ""

        note = "条文已换新版"
        r = c.post(f"/api/projects/{code}/kb-feedback", json={**_review(), "doc_id": x, "rating": "outdated",
                                                              "note": note})
        assert r.status_code == 200, r.text
        assert r.json()["recheck_marked"] is True
        v = _version(x)
        assert v.recheck_state == "pending" and v.recheck_reason == "使用反馈：顾问标过时 1 次、不对 0 次"

        # 再点覆盖：一处引用一人一条；已在待复查队列里的不重复标
        r = c.post(f"/api/projects/{code}/kb-feedback", json={**_review(), "doc_id": x, "rating": "wrong"})
        assert r.json()["mine"] == {x: "wrong", y: "useful"} and r.json()["recheck_marked"] is False
        with SessionLocal() as db:
            assert db.scalar(select(KnowledgeFeedback).where(KnowledgeFeedback.doc_id == x)).rating == "wrong"
            assert len(db.scalars(select(KnowledgeFeedback).where(KnowledgeFeedback.doc_id == x)).all()) == 1
            logs = db.scalars(select(AuditLog).where(AuditLog.action == "kb.feedback",
                                                     AuditLog.actor == member[0])).all()
        assert len(logs) == 3
        assert all(note not in lg.detail for lg in logs)
        flagged = next(json.loads(lg.detail) for lg in logs if '"outdated"' in lg.detail)
        assert flagged["note_length"] == 6 and len(flagged["note_sha"]) > 0 and "note" not in flagged
    finally:
        c.close()


def test_rejects_uncited_bad_rating_guard_hit_and_strict_note(people):
    x = _publish(people, f"反馈测试-丙{secrets.token_hex(2)}")
    code, member = _project()
    _write(code, member, 1, f"现状 [{x}]。", agent="report-writer")
    s_code, s_member = _project(tier="strict")
    s_mid = _message(s_code, s_member, [x])          # 严格档没有客户同意写不了章节，用对话回复测
    c, s = logged_in_client(app, member), logged_in_client(app, s_member)
    try:
        url = f"/api/projects/{code}/kb-feedback"
        assert c.post(url, json={**_review(), "doc_id": "L2-OPS-SOC-TPL-999", "rating": "useful"}).status_code == 404
        assert c.post(url, json={**_review(), "doc_id": x, "rating": "great"}).status_code == 422
        assert c.post(url, json={**_review(version=9), "doc_id": x, "rating": "useful"}).status_code == 404
        assert c.post(url, json={"doc_id": x, "rating": "useful"}).status_code == 422
        secret = "password=P@ssw0rd!"
        r = c.post(url, json={**_review(), "doc_id": x, "rating": "wrong", "note": secret})
        assert r.status_code == 422 and "机密级" in r.json()["detail"]
        assert _version(x).recheck_state == ""
        s_url = f"/api/projects/{s_code}/kb-feedback"
        r = s.post(s_url, json={"message_id": s_mid, "doc_id": x, "rating": "wrong", "note": "一句话"})
        assert r.status_code == 422 and "严格档" in r.json()["detail"]
        assert s.post(s_url, json={"message_id": s_mid, "doc_id": x, "rating": "useful"}).status_code == 200
        # 非成员看不到别的项目（404）
        assert c.get(s_url, params={"message_id": s_mid}).status_code == 404
        with SessionLocal() as db:
            assert db.scalar(select(KnowledgeFeedback).where(KnowledgeFeedback.doc_id == x, KnowledgeFeedback.rating == "wrong")) is None
            blocked = db.scalars(select(AuditLog).where(AuditLog.action == "kb.feedback",
                                                        AuditLog.result == "blocked", AuditLog.actor == member[0]))
            assert secret not in json.dumps([b.detail for b in blocked], ensure_ascii=False)
    finally:
        c.close()
        s.close()


def _message(code, member, refs):
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        u = db.scalar(select(User).where(User.username == member[0]))
        conv = Conversation(project_id=p.id, user_id=u.id, agent_code="consultant-lead", prompt_version="t")
        db.add(conv)
        db.flush()
        m = Message(conversation_id=conv.id, role="assistant", content="示范回答",
                    kb_refs=[{"doc_id": d, "title": "示范"} for d in refs])
        db.add(m)
        db.commit()
        return m.id


def test_chat_feedback_only_for_conversation_owner(people):
    x = _publish(people, f"反馈测试-丁{secrets.token_hex(2)}")
    code, member = _project()
    other = create_user(("consultant",), {code: "member"})
    mid = _message(code, member, [x])
    c, o = logged_in_client(app, member), logged_in_client(app, other)
    try:
        url = f"/api/projects/{code}/kb-feedback"
        r = c.get(url, params={"message_id": mid})
        assert r.status_code == 200 and r.json() == {"surface": "chat", "doc_ids": [x], "mine": {}}
        assert o.get(url, params={"message_id": mid}).status_code == 404
        assert o.post(url, json={"message_id": mid, "doc_id": x, "rating": "wrong"}).status_code == 404
        r = c.post(url, json={"message_id": mid, "doc_id": x, "rating": "wrong"})
        assert r.status_code == 200 and r.json()["recheck_marked"] is True
        assert _version(x).recheck_reason == "使用反馈：顾问标过时 0 次、不对 1 次"
    finally:
        c.close()
        o.close()


def test_old_flags_do_not_retrigger_after_recheck_handled(people):
    x = _publish(people, f"反馈测试-戊{secrets.token_hex(2)}")
    code, member = _project()
    _write(code, member, 1, f"现状 [{x}]。", agent="report-writer")
    c = logged_in_client(app, member)
    try:
        url = f"/api/projects/{code}/kb-feedback"
        assert c.post(url, json={**_review(), "doc_id": x, "rating": "outdated"}).json()["recheck_marked"] is True
        with SessionLocal() as db:                      # 专家复查通过（app/kb/autopublish.sample_check 的效果）
            item = db.scalar(select(KbItem).where(KbItem.doc_id == x))
            v = db.get(KbVersion, item.current_version_id)
            v.recheck_state, v.recheck_at = "passed", utcnow()
            db.commit()
            assert fb.mark_flagged(db, x) is False      # 复查之前的反馈不再算
        _write(code, member, 2, f"建议 [{x}]。", agent="report-writer")
        r = c.post(url, json={**_review(seq=2), "doc_id": x, "rating": "wrong"})
        assert r.json()["recheck_marked"] is True and _version(x).recheck_state == "pending"
    finally:
        c.close()


def test_usage_stats_citations_and_adoption(people):
    x, y = _publish(people, f"反馈测试-己{secrets.token_hex(2)}"), _publish(people, f"反馈测试-庚{secrets.token_hex(2)}")
    code, member = _project()
    _write(code, member, 1, f"现状 [{x}] [{y}]。", agent="report-writer")
    _write(code, member, 1, f"现状（顾问改写） [{y}]。")           # 顾问删掉 x 的引用
    _write(code, member, 2, f"建议 [{y}]。", agent="report-writer")
    _message(code, member, [x, y])
    c = logged_in_client(app, member)
    try:
        c.post(f"/api/projects/{code}/kb-feedback", json={**_review(seq=2), "doc_id": y, "rating": "useful",
                                                          "note": "很好用的模板"})
        assert c.get("/api/kb-usage").status_code == 403       # 顾问看不到看板（与 B4 引用计数一致）
    finally:
        c.close()
    c = logged_in_client(app, people["expert"])
    try:
        r = c.get("/api/kb-usage", params={"days": 30})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "很好用的模板" not in r.text and code not in r.text
        items = {i["doc_id"]: i for i in body["items"]}
        assert items[x]["section_citations"] == 1 and items[x]["chat_citations"] == 1
        assert items[x]["citations"] == 2 and items[x]["adoption_rate"] == 0.0
        assert items[y]["section_citations"] == 2 and items[y]["adoption_rate"] == 1.0
        assert items[y]["feedback"] == {"useful": 1, "outdated": 0, "wrong": 0}
        assert items[y]["title"].startswith("反馈测试-庚")
        assert body["totals"]["citations"] >= 5
        assert c.get("/api/kb-usage", params={"days": 0}).status_code == 422
    finally:
        c.close()


def test_delete_for_project(people):
    x = _publish(people, f"反馈测试-辛{secrets.token_hex(2)}")
    code, member = _project()
    _write(code, member, 1, f"现状 [{x}]。", agent="report-writer")
    c = logged_in_client(app, member)
    try:
        c.post(f"/api/projects/{code}/kb-feedback", json={**_review(), "doc_id": x, "rating": "useful"})
    finally:
        c.close()
    with SessionLocal() as db:
        pid = db.scalar(select(Project.id).where(Project.code == code))
        assert fb.delete_for_project(db, pid) == 1
        db.commit()
        assert db.scalar(select(KnowledgeFeedback).where(KnowledgeFeedback.project_id == pid)) is None


def test_chat_done_event_carries_message_id():
    """流式回复结束时带上存好的消息 id，界面不刷新也能对这条回复的参考知识点三键。"""
    code, member = _project()
    c = logged_in_client(app, member)
    try:
        done = [d for e, d in sse(ask(c, code, "SOC 值班交接要注意什么")) if e == "done"]
        assert done and isinstance(done[0]["message_id"], int)
        with SessionLocal() as db:
            m = db.get(Message, done[0]["message_id"])
            assert m.role == "assistant" and m.content
    finally:
        c.close()
