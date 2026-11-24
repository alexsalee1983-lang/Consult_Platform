"""四期 A17：知识使用可见与顾问反馈（在三期 B29 最小版上补齐，app/kb/feedback.py）。

- 写稿页 / 审阅页看到本段用了哪些知识：编号、标题、版本、来源依据、权威等级、是否在待复查；
- 标“过时 / 不对”除进待复查（B29）外，还作为“使用信号”进缺口池（B25②），同一处反馈改选不重复计；
- 知识健康（/api/kb/stats）出近 30 天三键计数、因反馈待复查的条目；
- 就绪度槽位标明待复查是顾问反馈引起的。
"""
import secrets

from sqlalchemy import select

from app.core.db import SessionLocal
from app.kb import readiness
from app.kb import review as rv
from app.kb.items import ItemMeta, VersionContent
from app.main import app
from app.models import KbGap, KbItem, User

from .conftest import logged_in_client
from .test_kb_feedback import _project, _review, _write, people  # noqa: F401  people 是 fixture


def _publish_basis(people, title):  # noqa: F811
    with SessionLocal() as db:
        u = {k: db.scalar(select(User).where(User.username == v[0])) for k, v in people.items()}
        m = ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type="TPL", stage="设计", capabilities=["管理"])
        v = rv.new_item(db, u["author"], m, VersionContent(
            title=title, body=f"## {title}\n\n示范做法。", source_type="public", source_detail="公开资料：示范",
            ref_document="信息安全技术 网络安全等级保护基本要求", ref_version="2019", ref_code="GB/T 22239-2019"))
        rv.submit(db, u["author"], v)
        rv.review(db, u["expert"], v, "content", "approve")
        rv.review(db, u["admin"], v, "compliance", "approve")
        return v.item.doc_id


def test_section_lists_used_knowledge_with_version_and_authority(people):  # noqa: F811
    x = _publish_basis(people, f"A17-U1-{secrets.token_hex(2)}")
    code, member = _project()
    _write(code, member, 1, f"现状与建议 [{x}]。", agent="report-writer")
    c = logged_in_client(app, member)
    try:
        r = c.get(f"/api/projects/{code}/kb-feedback", params=_review())
        assert r.status_code == 200, r.text
        items = {i["doc_id"]: i for i in r.json()["items"]}
        it = items[x]
        assert it["title"].startswith("A17-U1-") and it["version"] == 1 and it["layer"] == "L2"
        assert it["authority_level"] == 2 and it["authority_label"] == "标准"
        assert it["ref_code"] == "GB/T 22239-2019" and it["ref_version"] == "2019"
        assert it["recheck_state"] == "" and it["flagged"] is False
        assert "body" not in it                                  # 只给元数据，不给正文
    finally:
        c.close()


def test_flag_goes_to_gap_pool_and_knowledge_health(people):  # noqa: F811
    x = _publish_basis(people, f"A17-U2-{secrets.token_hex(2)}")
    code, member = _project()
    _write(code, member, 1, f"现状 [{x}]。", agent="report-writer")
    c = logged_in_client(app, member)
    try:
        url = f"/api/projects/{code}/kb-feedback"
        r = c.post(url, json={**_review(), "doc_id": x, "rating": "outdated", "note": "条文换版了"})
        assert r.status_code == 200, r.text
        it = next(i for i in r.json()["items"] if i["doc_id"] == x)
        assert it["recheck_state"] == "pending" and it["flagged"] is True
        c.post(url, json={**_review(), "doc_id": x, "rating": "wrong"})       # 同一处改选：不重复计
        with SessionLocal() as db:
            g = db.scalar(select(KbGap).where(KbGap.gap_key == x))
            assert g is not None and g.kinds == {"usage": 1} and len(g.events) == 1
            assert g.authority == 2 and "条文" not in str(g.events)
        assert c.get("/api/kb/stats").status_code == 403                    # 顾问看不到知识健康
    finally:
        c.close()
    c = logged_in_client(app, people["expert"])
    try:
        r = c.get("/api/kb/stats")
        assert r.status_code == 200, r.text
        f = r.json()["feedback"]
        assert set(f["counts_30d"]) == {"useful", "outdated", "wrong"} and f["counts_30d"]["wrong"] >= 1
        assert f["flagged_recheck"] >= 1
        top = {t["doc_id"]: t for t in f["top_flagged"]}
        assert top[x]["wrong"] == 1 and top[x]["recheck_state"] == "pending"
        assert "条文换版了" not in r.text and code not in r.text
    finally:
        c.close()


def test_readiness_slot_marks_feedback_recheck(people):  # noqa: F811
    x = _publish_basis(people, f"A17-U3-{secrets.token_hex(2)}")
    key = f"a17-{secrets.token_hex(3)}"
    with SessionLocal() as db:
        item = db.scalar(select(KbItem).where(KbItem.doc_id == x))
        item.import_key = key
        db.commit()
        assert readiness._slot_states(db, {key})[key]["reason"] == ""
    code, member = _project()
    _write(code, member, 1, f"现状 [{x}]。", agent="report-writer")
    c = logged_in_client(app, member)
    try:
        c.post(f"/api/projects/{code}/kb-feedback", json={**_review(), "doc_id": x, "rating": "wrong"})
    finally:
        c.close()
    with SessionLocal() as db:
        s = readiness._slot_states(db, {key})[key]
    assert s["state"] == "recheck" and s["reason"] == "feedback"
