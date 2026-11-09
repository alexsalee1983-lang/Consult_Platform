"""对话接入知识库：提问前检索，命中条目附进 Agent 系统提示，回复记录引用；按授权、可见范围、机密级拦截。"""
import json

import httpx
import pytest
from sqlalchemy import select

from app.agents import knowledge
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import review as rv
from app.kb.items import ItemMeta, VersionContent
from app.main import app
from app.models import AuditLog, User
from app.services import gateway

from .conftest import create_user, logged_in_client


def _user(roles):
    name, *_ = create_user(roles)
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == name))


def _publish(title, body, *, layer="L2", product="SOC", doc_type="MTD", stage="设计", ref=None,
             classification="internal", visible=None) -> str:
    author, expert, admin = _user(("knowledge_expert",)), _user(("knowledge_expert",)), _user(("knowledge_admin",))
    with SessionLocal() as db:
        c = VersionContent(title=title, body=body, source_type="public", source_detail="公开资料")
        if ref:
            c.ref_document, c.ref_version, c.ref_status = ref
        v = rv.new_item(db, db.merge(author), ItemMeta(layer=layer, domain="OPS", product=product, doc_type=doc_type,
                                                       stage=stage, capabilities=["运营"],
                                                       classification=classification,
                                                       visible_roles=visible or ["consultant", "admin"]), c)
        rv.submit(db, db.merge(author), v)
        rv.review(db, db.merge(expert), v, "content", "approve")
        rv.review(db, db.merge(admin), v, "compliance", "approve")
        db.refresh(v)
        return v.item.doc_id


@pytest.fixture(scope="module")
def kb():
    return {
        "night": _publish("对话测试-夜间值守做法", "## 夜间值守\n\n无夜班客户由托管方初筛，一级事件电话升级轮值负责人。"),
        "secret": _publish("对话测试-受限异议应答", "## 夜间值守应答\n\n夜间值守人力不足时的内部应答口径。",
                           doc_type="OBJ", classification="restricted", visible=["admin"]),
        "basis": _publish("对话测试-集中管控要求", "## 8.1.5 集中管控\n\n应对夜间值守发现的安全事件进行集中分析。",
                          layer="L4", product="GEN", doc_type="REF", stage="通用",
                          ref=("GB/T 22239", "2019", "current")),
    }


class _Model:
    def __init__(self):
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4.1-flash"}]})
        self.requests.append(json.loads(request.content))
        lines = ['data: {"choices": [{"delta": {"content": "按 [L2] 条目回答"}}]}', "data: [DONE]"]
        return httpx.Response(200, content="\n\n".join(lines).encode(), headers={"content-type": "text/event-stream"})

    @property
    def system(self) -> str:
        return self.requests[-1]["messages"][0]["content"]


@pytest.fixture()
def model(monkeypatch):
    m = _Model()
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
    gateway.set_transport(httpx.MockTransport(m.handler))
    yield m
    gateway.set_transport(None)


@pytest.fixture()
def client():
    c = logged_in_client(app, create_user(("consultant",), {"BANK-A": "member"}))
    yield c
    c.__exit__(None, None, None)


def _ask(client, agent, message):
    conv = client.post("/api/projects/BANK-A/conversations", json={"agent_code": agent}).json()
    r = client.post(f"/api/conversations/{conv['id']}/messages", json={"message": message})
    return conv, r


def _done(r) -> dict:
    block = next(b for b in r.text.split("\n\n") if b.startswith("event: done"))
    return json.loads(block.split("data: ", 1)[1])


def test_product_code_mapping():
    assert knowledge.product_code("SOC 运营体系设计") == "SOC"
    assert knowledge.product_code("网络安全治理规划") == ""      # 一期没有这个产品的知识，不按产品过滤


def test_knowledge_injected_and_references_recorded(kb, model, client):
    conv, r = _ask(client, "consultant-lead", "客户没有夜班，夜间值守怎么安排？")
    assert r.status_code == 200
    system = model.system
    assert f"[{kb['night']}]" in system and "托管方初筛" in system
    assert kb["secret"] not in system                                      # 受限条目顾问看不到，也不注入
    refs = _done(r)["references"]
    assert kb["night"] in [x["doc_id"] for x in refs]
    assert all(set(x) == {"doc_id", "title", "heading", "layer", "citation"} for x in refs)   # 引用不含正文
    msgs = client.get(f"/api/conversations/{conv['id']}").json()["messages"]
    assert msgs[1]["role"] == "assistant" and kb["night"] in [x["doc_id"] for x in msgs[1]["references"]]
    assert msgs[0]["references"] == []


def test_basis_only_for_agents_with_grant(kb, model, client):
    _ask(client, "compliance-basis", "夜间值守发现的安全事件要怎么集中分析？依据是什么？")
    assert f"【依据：GB/T 22239 2019 8.1.5 集中管控｜{kb['basis']}】" in model.system
    with SessionLocal() as db:
        calls = [json.loads(a.detail)["tool"] for a in db.scalars(
            select(AuditLog).where(AuditLog.action == "tool.call").order_by(AuditLog.id.desc()).limit(2))]
    assert sorted(calls) == ["basis.lookup", "kb.search"]
    _ask(client, "consultant-lead", "夜间值守怎么安排？")
    with SessionLocal() as db:
        last = json.loads(db.scalars(select(AuditLog).where(AuditLog.action == "tool.call")
                                     .order_by(AuditLog.id.desc())).first().detail)
    assert last["tool"] == "kb.search"                                     # 咨询师 Agent 没有依据查询授权


def test_no_hits_says_so(kb, model, client):
    _, r = _ask(client, "consultant-lead", "完全无关的问题甲乙丙丁戊")
    assert r.status_code == 200 and "本轮没有检索到相关知识库条目" in model.system
    assert _done(r)["references"] == []


def test_blocked_question_does_not_search(kb, model, client):
    with SessionLocal() as db:
        before = db.scalar(select(AuditLog.id).order_by(AuditLog.id.desc()).limit(1))
    _, r = _ask(client, "consultant-lead", "核心交换机 10.1.20.1 管理口令 Abc@12345 夜间值守")
    assert r.status_code == 422 and model.requests == []
    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.id > before)).all()
    assert "kb.search" not in [a.action for a in rows]
    tools_called = [json.loads(a.detail)["tool"] for a in rows if a.action == "tool.call"]
    assert "kb.search" not in tools_called                                   # 只有读项目卡（project.read）
