"""知识审批：草稿、提交、双人审核规则、发布切片、修订替代、退回、下线、复审、可见范围。"""
import json
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.kb import meta
from app.main import app
from app.models import AuditLog, KbChunk, KbItem, KbVersion

from .conftest import create_user, logged_in_client, on_postgres, on_s3

BODY = "## 交接班\n\n交接班时必须核对未闭环的一级事件和升级记录。\n\n## 夜间\n\n无夜班客户由托管方初筛，一级事件电话升级轮值负责人。"


def _payload(title="值班管理规程", body=BODY, source_type="public", **meta_kw):
    m = {"layer": "L2", "domain": "OPS", "product": "SOC", "doc_type": "POL", "stage": "设计",
         "capabilities": ["运营"], **meta_kw}
    return {"meta": m, "content": {"title": title, "body": body, "source_type": source_type,
                                   "source_detail": "公开资料整理"}}


@pytest.fixture(scope="module")
def people():
    made = {name: logged_in_client(app, create_user(roles)) for name, roles in {
        "author": ("knowledge_expert",), "expert2": ("knowledge_expert",), "expert3": ("knowledge_expert",),
        "admin": ("knowledge_admin",), "admin2": ("knowledge_admin",), "consultant": ("consultant",)}.items()}
    yield made
    for c in made.values():
        c.__exit__(None, None, None)


def _publish(people, payload=None) -> dict:
    v = people["author"].post("/api/kb/items", json=payload or _payload()).json()
    assert people["author"].post(f"/api/kb/versions/{v['id']}/submit").status_code == 200
    assert people["expert2"].post(f"/api/kb/versions/{v['id']}/review",
                                  json={"role": "content", "decision": "approve"}).status_code == 200
    r = people["admin"].post(f"/api/kb/versions/{v['id']}/review", json={"role": "compliance", "decision": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "published", r.text
    return r.json()


# ---- 草稿 ----

def test_create_public_draft_and_rules(people):
    r = people["author"].post("/api/kb/items", json=_payload())
    assert r.status_code == 201
    v = r.json()
    assert v["status"] == "draft" and v["deid_status"] == "not_needed" and v["doc_id"].startswith("L2-OPS-SOC-POL-")
    assert people["consultant"].post("/api/kb/items", json=_payload()).status_code == 403
    r = people["author"].post("/api/kb/items", json=_payload(source_type="project"))
    assert r.status_code == 422 and "公开资料" in r.json()["detail"]                       # 非公开资料必须先投料
    r = people["author"].post("/api/kb/items", json=_payload(body="核心交换机 10.1.20.1 管理口令 Abc@12345"))
    assert r.status_code == 422 and "机密级" in r.json()["detail"]
    assert people["author"].post("/api/kb/items", json=_payload(stage="汇报")).status_code == 422
    assert people["consultant"].get(f"/api/kb/versions/{v['id']}").status_code == 404     # 未发布的草稿顾问看不到


def test_edit_only_drafts(people):
    v = people["author"].post("/api/kb/items", json=_payload()).json()
    r = people["author"].patch(f"/api/kb/versions/{v['id']}", json={"summary": "四级分级与升级路径"})
    assert r.status_code == 200 and r.json()["summary"] == "四级分级与升级路径"
    assert people["author"].patch(f"/api/kb/versions/{v['id']}", json={"body": "口令 Abc@12345 x"}).status_code == 422
    people["author"].post(f"/api/kb/versions/{v['id']}/submit")
    assert people["author"].patch(f"/api/kb/versions/{v['id']}", json={"summary": "x"}).status_code == 409


# ---- 双人审核 ----

def test_two_person_review_rules_then_publish(people):
    v = people["author"].post("/api/kb/items", json=_payload(title="告警分级方法")).json()
    vid = v["id"]
    review = lambda who, role, d="approve": people[who].post(f"/api/kb/versions/{vid}/review",  # noqa: E731
                                                             json={"role": role, "decision": d})
    assert review("expert2", "content").status_code == 409                       # 还没提交
    people["author"].post(f"/api/kb/versions/{vid}/submit")
    assert review("author", "content").status_code == 403                        # 不能审自己的
    assert review("admin", "content").status_code == 403                         # 知识管理员不能做内容审核
    assert review("expert2", "compliance").status_code == 403                    # 知识专家不能做合规审核
    assert review("consultant", "content").status_code == 404                    # 顾问看不到待审版本
    r = review("expert2", "content")
    assert r.status_code == 200 and r.json()["status"] == "in_review"            # 一人通过还不发布
    assert review("expert2", "content").status_code == 409                       # 同一人不能审两次
    assert review("expert3", "content").status_code == 409                       # 内容审核已有人通过
    r = review("admin", "compliance")
    assert r.json()["status"] == "published"
    with SessionLocal() as db:
        ver = db.get(KbVersion, vid)
        assert ver.item.current_version_id == vid and ver.published_at is not None
        assert ver.review_date == meta.default_review_date("L2", date.today())
        chunks = db.scalars(select(KbChunk).where(KbChunk.version_id == vid).order_by(KbChunk.seq)).all()
        assert [c.heading for c in chunks] == ["交接班", "夜间"]
        reasons = [json.loads(a.detail).get("reason") for a in db.scalars(
            select(AuditLog).where(AuditLog.action == "kb.review", AuditLog.result == "blocked"))]
    assert {"self_review", "role_mismatch", "duplicate", "role_done"} <= set(reasons)
    # 发布后顾问能看到
    assert people["consultant"].get(f"/api/kb/versions/{vid}").json()["reviews"] == []   # 审核信息不给顾问
    assert people["consultant"].get(f"/api/kb/items/{r.json()['doc_id']}").status_code == 200


def test_reject_is_final_and_revision_supersedes(people):
    v1 = _publish(people, _payload(title="事件响应流程"))
    doc_id = v1["doc_id"]
    a = people["author"]
    r = a.post(f"/api/kb/items/{doc_id}/versions",
               json={"content": {"title": "事件响应流程", "source_type": "public", "change_note": "补充夜间升级"}})
    assert r.status_code == 201 and r.json()["version"] == 2 and r.json()["body"] == BODY   # 默认沿用现行正文
    v2 = r.json()["id"]
    assert a.post(f"/api/kb/items/{doc_id}/versions",
                  json={"content": {"title": "x", "source_type": "public"}}).status_code == 409   # 已有未完成的草稿
    a.patch(f"/api/kb/versions/{v2}", json={"body": BODY + "\n\n夜间升级须 15 分钟内电话通知。"})
    a.post(f"/api/kb/versions/{v2}/submit")
    detail = people["expert2"].get(f"/api/kb/versions/{v2}").json()
    assert any(line.startswith("+夜间升级") for line in detail["diff"])               # 与现行版本的差异
    people["admin"].post(f"/api/kb/versions/{v2}/review", json={"role": "compliance", "decision": "reject",
                                                                "comment": "来源说明不足"})
    assert people["expert2"].get(f"/api/kb/versions/{v2}").json()["status"] == "rejected"
    assert a.patch(f"/api/kb/versions/{v2}", json={"summary": "x"}).status_code == 409    # 退回是终态
    # 再建第 3 版，通过后替代第 1 版
    v3 = a.post(f"/api/kb/items/{doc_id}/versions",
                json={"content": {"title": "事件响应流程", "source_type": "public", "change_note": "按退回意见修改",
                                  "body": BODY + "\n\n依据：公开行业指引。"}}).json()["id"]
    a.post(f"/api/kb/versions/{v3}/submit")
    people["expert3"].post(f"/api/kb/versions/{v3}/review", json={"role": "content", "decision": "approve"})
    people["admin2"].post(f"/api/kb/versions/{v3}/review", json={"role": "compliance", "decision": "approve"})
    with SessionLocal() as db:
        statuses = {v.version: v.status for v in db.scalar(select(KbItem).where(KbItem.doc_id == doc_id)).versions}
    assert statuses == {1: "superseded", 2: "rejected", 3: "published"}
    c = people["consultant"]
    assert c.get(f"/api/kb/versions/{v1['id']}").status_code == 404                     # 旧版本顾问看不到
    assert [v["version"] for v in c.get(f"/api/kb/items/{doc_id}").json()["versions"]] == [3]
    assert len(people["expert2"].get(f"/api/kb/items/{doc_id}").json()["versions"]) == 3


def test_retire_reconfirm_and_review_due(people):
    v = _publish(people, _payload(title="值班交接记录模板", doc_type="TPL"))
    doc_id = v["doc_id"]
    with SessionLocal() as db:
        db.get(KbVersion, v["id"]).review_date = date.today() - timedelta(days=1)
        db.commit()
    due = [x["doc_id"] for x in people["expert2"].get("/api/kb/review-due").json()]
    assert doc_id in due
    assert people["consultant"].get("/api/kb/review-due").status_code == 403
    assert people["admin"].post(f"/api/kb/items/{doc_id}/reconfirm").status_code == 403    # 复审须知识专家
    r = people["expert2"].post(f"/api/kb/items/{doc_id}/reconfirm")
    assert r.json()["review_date"] == meta.default_review_date("L2", date.today()).isoformat()
    assert doc_id not in [x["doc_id"] for x in people["expert2"].get("/api/kb/review-due").json()]

    assert people["expert2"].post(f"/api/kb/items/{doc_id}/retire", json={}).status_code == 403
    assert people["admin"].post(f"/api/kb/items/{doc_id}/retire", json={"reason": "已被新模板替代"}).status_code == 200
    assert people["consultant"].get(f"/api/kb/items/{doc_id}").status_code == 404
    assert doc_id not in [i["doc_id"] for i in people["consultant"].get("/api/kb/items").json()]
    assert people["expert2"].get(f"/api/kb/items/{doc_id}").json()["retired"] is True


def test_restricted_items_hidden_from_consultants(people):
    v = _publish(people, _payload(title="受限：异议应答口径", doc_type="OBJ", classification="restricted",
                                  visible_roles=["admin"]))
    assert people["consultant"].get(f"/api/kb/items/{v['doc_id']}").status_code == 404
    assert people["expert2"].get(f"/api/kb/items/{v['doc_id']}").status_code == 200


def test_review_queue(people):
    v = people["author"].post("/api/kb/items", json=_payload(title="待审队列测试")).json()
    people["author"].post(f"/api/kb/versions/{v['id']}/submit")
    assert v["id"] in [x["id"] for x in people["admin"].get("/api/kb/review-queue").json()]
    assert people["consultant"].get("/api/kb/review-queue").status_code == 403


@on_postgres
def test_published_chunks_searchable_and_similar_items(people):
    from sqlalchemy import text

    _publish(people, _payload(title="夜间值守与升级路径"))
    with SessionLocal() as db:
        n = db.execute(text("SELECT count(*) FROM kb_chunks c JOIN kb_versions v ON v.id = c.version_id "
                            "WHERE v.status = 'published' AND c.tsv @@ plainto_tsquery('zh', '夜间 升级')")).scalar()
    assert n >= 1
    v = people["author"].post("/api/kb/items", json=_payload(title="夜间值守升级")).json()
    similar = people["expert2"].get(f"/api/kb/versions/{v['id']}").json()["similar"]
    assert similar and all(s["doc_id"] != v["doc_id"] for s in similar)


# ---- 从投料建条目（接真实对象存储）----

@on_s3
def test_item_from_deid_passed_upload(people, monkeypatch):
    from app.services import jobs

    up = people["author"].post("/api/kb/intake", files={"file": (
        "复盘.md", "# 江南农商行值班复盘（审批测试）\n\n江南农商行的张总要求一级事件 15 分钟内响应。".encode(), "text/markdown")})
    uid = up.json()["id"]
    with SessionLocal() as db:
        while jobs.run_once(db, "test-worker", kinds=["kb.parse", "kb.deid"]):
            pass
    p = _payload(title="夜间一级事件响应时限", source_type="project")
    p["upload_id"] = uid
    assert people["author"].post("/api/kb/items", json=p).status_code == 409             # 去标识还没通过
    assert people["admin"].post(f"/api/kb/intake/{uid}/deid/decision", json={"decision": "pass"}).status_code == 200
    p["content"]["body"] = ""
    r = people["author"].post("/api/kb/items", json=p)
    assert r.status_code == 201
    v = r.json()
    assert v["deid_status"] == "passed" and "【客户1】" in v["body"] and "江南农商行" not in v["body"]
