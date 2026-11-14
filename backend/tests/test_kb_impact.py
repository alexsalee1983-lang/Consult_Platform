"""二期 B4：发布影响——引用的知识条目更新或下线后提醒复核；知识侧只看引用计数。"""
import secrets

from sqlalchemy import select

from app.core.db import SessionLocal
from app.kb import review as rv
from app.kb.items import ItemMeta, VersionContent
from app.main import app
from app.models import KbItem, User

from .conftest import create_user, logged_in_client
from .test_deliverables import _base, _new_project


def _content(title: str, body: str) -> VersionContent:
    return VersionContent(title=title, body=body, source_type="public", source_detail="公开资料",
                          ref_document="GB/T 22239", ref_version="2019", ref_status="current")


def test_updated_and_retired_items_flag_sections_until_rewritten():
    expert, expert2, admin = (create_user(r) for r in (("knowledge_expert",), ("knowledge_expert",),
                                                       ("knowledge_admin",)))
    with SessionLocal() as db:
        u = {n: db.scalar(select(User).where(User.username == n)) for n in (expert[0], expert2[0], admin[0])}

        def publish(v):
            rv.submit(db, u[expert[0]], v)
            rv.review(db, u[expert2[0]], v, "content", "approve")
            rv.review(db, u[admin[0]], v, "compliance", "approve")
            db.refresh(v)

        v1 = rv.new_item(db, u[expert[0]], ItemMeta(layer="L4", domain="OPS", product="GEN", doc_type="REF",
                                                    stage="通用", capabilities=["运营"], visible_roles=["consultant"]),
                         _content(f"影响测试 {secrets.token_hex(2)}", "## 8.1.5 集中管控\n\n应集中分析。"))
        publish(v1)
        doc_id = v1.item.doc_id

    code = _new_project()
    member = create_user(("consultant",), {code: "member"})
    with logged_in_client(app, member) as c, logged_in_client(app, expert) as k, \
            logged_in_client(app, create_user(("consultant",))) as other:
        c.put(f"{_base(code)}/outline", json={"titles": ["现状", "差距"]})
        assert c.put(f"{_base(code)}/sections/1", json={"body": f"日志应集中分析 [{doc_id}]。"}).status_code == 201
        c.put(f"{_base(code)}/sections/2", json={"body": "差距待定。[推断]"})
        url = f"/api/projects/{code}/knowledge-updates"
        assert c.get(url).json() == []
        assert k.get(f"/api/kb/items/{doc_id}/usage").json() == {"projects": 1, "deliverables": 1, "sections": 1}
        assert c.get(f"/api/kb/items/{doc_id}/usage").status_code == 403          # 顾问看不到引用计数
        assert other.get(url).status_code == 404                                  # 非成员

        with SessionLocal() as db:                                                # 条目发布第 2 版
            item = db.scalar(select(KbItem).where(KbItem.doc_id == doc_id))
            v2 = rv.new_version(db, db.scalar(select(User).where(User.username == expert[0])), item, _content("影响测试（修订）", "## 8.1.5 集中管控\n\n应集中分析并留存。"))
            rv.submit(db, db.scalar(select(User).where(User.username == expert[0])), v2)
            rv.review(db, db.scalar(select(User).where(User.username == expert2[0])), v2, "content", "approve")
            rv.review(db, db.scalar(select(User).where(User.username == admin[0])), v2, "compliance", "approve")

        rows = c.get(url).json()
        assert [(r["deliverable"], r["section"], r["doc_id"], r["change"], r["current_version"]) for r in rows] == \
            [("D4", 1, doc_id, "updated", 2)]
        d = c.get(_base(code)).json()
        assert d["totals"]["knowledge_updates"] == 1
        assert d["sections"][0]["knowledge_updates"][0]["change"] == "updated" and d["sections"][1]["knowledge_updates"] == []

        # 顾问复核后改写本章：提醒消失
        assert c.put(f"{_base(code)}/sections/1", json={"body": f"日志应集中分析并留存 [{doc_id}]。"}).status_code == 201
        assert c.get(url).json() == []

        with SessionLocal() as db:                                                # 条目下线
            item = db.scalar(select(KbItem).where(KbItem.doc_id == doc_id))
            rv.retire(db, db.scalar(select(User).where(User.username == admin[0])), item, "标准已废止")
        rows = c.get(url).json()
        assert [(r["section"], r["change"]) for r in rows] == [(1, "retired")]
        assert k.get("/api/kb/items/L4-NOPE-0000/usage").status_code == 404
