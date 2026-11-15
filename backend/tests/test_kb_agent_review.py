"""知识审核 Agent 与自动发布（二期 B11）：程序规则 + Agent 逐项检查，全过自动发布并抽检，存疑转人工。"""
import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import agent_review, autopublish, importer, sources
from app.kb import review as rv
from app.kb.files import parse_entry
from app.models import AuditLog, Job, KbItem, KbVersion, User
from app.services import gateway

from .conftest import create_user

LAW = "第一条 为了保障示范审核安全，制定本法。\n第二条 运营者应当每年开展一次风险评估。\n"
PASS = {"checks": {k: {"pass": True, "reason": "与原文一致"} for k in agent_review.CHECKS}, "issues": [],
        "conclusion": "通过"}


class _Model:
    def __init__(self, reply: dict):
        self.reply, self.requests = json.dumps(reply, ensure_ascii=False), []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4.1-flash"}]})
        self.requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": self.reply}}], "usage": {}})


@pytest.fixture()
def model(monkeypatch):
    monkeypatch.setattr(get_settings(), "kb_sample_fail_max", 1.0)       # 其他用例留下的抽检结果不影响

    def use(reply: dict) -> _Model:
        m = _Model(reply)
        monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
        gateway.set_transport(httpx.MockTransport(m.handler))
        return m
    yield use
    gateway.set_transport(None)


@pytest.fixture()
def lib():
    """作者、知识专家、知识管理员，和一部已发布的示范法（每个用例名称不同，用完下线）。"""
    names = [create_user(r)[0] for r in (("knowledge_expert",), ("knowledge_expert",), ("knowledge_admin",))]
    with SessionLocal() as db:
        people = [db.scalar(select(User).where(User.username == n)) for n in names]
        title = f"示范审核法{uuid.uuid4().hex[:6]}"
        entries = sources.split_doc(sources.Doc(file="l.txt", title=title, code="主席令第9号", version="2029"),
                                    LAW.encode())[0]
        author, expert, admin = people
        for e in entries:
            importer.import_entry(db, author, e)
            v = db.scalar(select(KbItem).where(KbItem.import_key == e.key)).versions[-1]
            rv.submit(db, author, v)
            rv.review(db, expert, v, "content", "approve")
            rv.review(db, admin, v, "compliance", "approve")
    yield people, title
    with SessionLocal() as db:
        for e in entries:
            db.scalar(select(KbItem).where(KbItem.import_key == e.key)).retired = True
        db.commit()


def _submitted(db, author, law: str, body: str, level3: bool = False, **extra) -> KbVersion:
    head = {"key": f"t-{uuid.uuid4().hex[:8]}", "title": "示范审核法第二条解读", "layer": "L4", "domain": "CMP",
            "product": "GEN", "doc_type": "REF", "stage": "通用", "capabilities": "[管理]", "source_type": "public",
            "source_detail": "公开资料：示范", "ref_document": law, "ref_version": "2029", "ref_status": "current",
            "summary": "第二条要求每年评估", "applicability": "适用于示范行业运营者", **extra}
    e = parse_entry("---\n" + "\n".join(f"{k}: {v}" for k, v in head.items() if v != "") + f"\n---\n\n{body}\n")
    r = importer.import_entry(db, author, e)
    assert r.action == "created", r.message
    v = db.get(KbVersion, r.version_id)
    if level3:                                   # 模拟去标识已确认的专家经验（只在测试里直接改取值）
        v.source_type, v.deid_status = "expert", "passed"
        db.commit()
    rv.submit(db, author, v)
    return v


BODY = "## 第二条 风险评估\n\n依据第二条，运营者应当每年开展一次风险评估，可结合年度检查一并进行。"


def test_pass_publishes_and_samples(lib, model, monkeypatch):
    (author, expert, admin), law = lib
    m = model(PASS)
    monkeypatch.setattr(get_settings(), "kb_sample_rate_agent", 1.0)
    with SessionLocal() as db:
        v = _submitted(db, db.merge(author), law, BODY)
        job = db.scalar(select(Job).where(Job.kind == "kb.agent_review").order_by(Job.id.desc()))
        assert json.loads(job.payload) == {"version_id": v.id}
        out = agent_review.run(db, v)
        assert out.status == "published" and out.sampled and not out.rules_failed
        assert (v.status, v.auto_review, v.sample_state) == ("published", "agent", "pending")
        r = next(x for x in v.reviews if x.review_role == "agent")
        assert r.reviewer == "agent:kb-reviewer" and r.decision == "approve" and r.findings["conclusion"] == "通过"
        sent = m.requests[0]["messages"]
        assert "运营者应当每年开展一次风险评估" in sent[-1]["content"]           # 引用到的依据原文交给了 Agent
        assert "审核清单" in sent[0]["content"]
        log = db.scalar(select(AuditLog).where(AuditLog.action == "kb.agent_review").order_by(AuditLog.id.desc()))
        detail = json.loads(log.detail)
        assert detail["prompt_version"] and detail["outcome"] == "published" and "风险评估" not in log.detail
        assert agent_review.run(db, v).status == "skipped"                     # 重复执行不再审
        with pytest.raises(rv.ReviewError, match="不能抽检自己导入的版本"):
            autopublish.sample_check(db, db.merge(author), v, "pass")
        assert autopublish.sample_check(db, db.merge(expert), v, "fail", "年度频次原文没有").sample_state == "failed"
        assert autopublish.sample_stats(db, "agent")["failed"] >= 1


def test_flag_stays_for_humans(lib, model):
    (author, expert, admin), law = lib
    reply = {**PASS, "checks": {**PASS["checks"], "no_addition": {"pass": False, "reason": "存疑：年度检查是添加"}},
             "issues": [{"severity": "阻断", "location": "第二条", "problem": "加入了原文没有的做法", "suggestion": "删去"}],
             "conclusion": "存疑"}
    model(reply)
    with SessionLocal() as db:
        v = _submitted(db, db.merge(author), law, BODY)
        out = agent_review.run(db, v)
        assert out.status == "manual" and v.status == "in_review" and v.auto_review == ""
        r = next(x for x in v.reviews if x.review_role == "agent")
        assert r.decision == "flag" and r.findings["issues"][0]["severity"] == "阻断"
        assert not r.findings["checks"]["no_addition"]["pass"]
        rv.review(db, db.merge(expert), v, "content", "approve")               # 人工两项照常发布
        rv.review(db, db.merge(admin), v, "compliance", "approve")
        assert v.status == "published" and v.auto_review == ""


def test_rules_block_and_demo(lib, model, monkeypatch):
    (author, *_), law = lib
    with SessionLocal() as db:
        v = _submitted(db, db.merge(author), law, BODY + "落实后即通过等保三级测评。", applicability="")
        failed, _ = agent_review.rule_failures(db, v)
        assert failed == ["boundary", "metadata"]
        out = agent_review.run(db, v)                                          # 没接模型：演示模式
        assert out.status == "manual" and out.agent == "skipped" and v.status == "in_review"
    model(PASS)
    monkeypatch.setattr(get_settings(), "kb_agent_auto_publish", False)
    with SessionLocal() as db:
        w = _submitted(db, db.merge(author), law, BODY)
        assert agent_review.run(db, w).status == "manual"                      # 暂停时 Agent 通过也转人工
        assert "KB_AGENT_AUTO_PUBLISH=false" in next(x for x in w.reviews if x.review_role == "agent").comment


def test_not_eligible(lib):
    (author, *_), law = lib
    with SessionLocal() as db:
        v = _submitted(db, db.merge(author), law, BODY)
        v.source_type = "expert"                                               # 只看内存里的取值，不提交
        assert not agent_review.eligible(v) and agent_review.run(db, v).status == "skipped"


def test_level3_agent_advises_one_expert_decides(lib, model, monkeypatch):
    """第三级（B12）：去标识确认后的项目沉淀、专家经验，Agent 只出意见，一位知识专家拍板，不要合规审核。"""
    (author, expert, admin), law = lib
    monkeypatch.setattr(get_settings(), "kb_agent_auto_publish", False)      # 第三级不受自动发布开关影响
    model(PASS)
    with SessionLocal() as db:
        v = _submitted(db, db.merge(author), law, BODY, level3=True)
        assert rv.level3(v) and rv.required_roles(v) == ("content",)
        job = db.scalar(select(Job).where(Job.kind == "kb.agent_review").order_by(Job.id.desc()))
        assert json.loads(job.payload) == {"version_id": v.id}
        out = agent_review.run(db, v)
        assert out.status == "manual" and v.status == "in_review" and v.auto_review == "" and v.sample_state == ""
        r = next(x for x in v.reviews if x.review_role == "agent")
        assert r.decision == "approve" and r.findings["level"] == 3 and "只出意见" in r.comment
        log = db.scalar(select(AuditLog).where(AuditLog.action == "kb.agent_review").order_by(AuditLog.id.desc()))
        assert json.loads(log.detail)["outcome"] == "advice" and "风险评估" not in log.detail
        with pytest.raises(rv.ReviewError) as e:
            rv.review(db, db.merge(admin), v, "compliance", "approve")
        assert e.value.status == 409
        with pytest.raises(rv.ReviewError):
            rv.review(db, db.merge(author), v, "content", "approve")          # 作者不能审自己的版本
        rv.review(db, db.merge(expert), v, "content", "approve")
        assert v.status == "published" and v.auto_review == ""


def test_level3_flag_still_human(lib, model):
    (author, expert, _), law = lib
    reply = {**PASS, "checks": {**PASS["checks"], "no_addition": {"pass": False, "reason": "存疑"}},
             "issues": [{"severity": "阻断", "location": "第二条", "problem": "加入了原文没有的做法", "suggestion": "删去"}],
             "conclusion": "存疑"}
    model(reply)
    with SessionLocal() as db:
        v = _submitted(db, db.merge(author), law, BODY, level3=True)
        assert agent_review.run(db, v).status == "manual"
        r = next(x for x in v.reviews if x.review_role == "agent")
        assert r.decision == "flag" and v.status == "in_review"
        rv.review(db, db.merge(expert), v, "content", "approve")               # 意见供参考，拍板仍由人
        assert v.status == "published"
