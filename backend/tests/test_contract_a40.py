"""四期 A40：要点卡规则检查（只显示例外）、需求澄清清单、纪要回填与变更单。虚拟客户 BANK-A 风格数据。"""
import io
import json

import pytest
from docx import Document
from sqlalchemy import select

from app.agents import call
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Job
from app.project import contract_checks as cc
from app.services import gateway, jobs, storage

from .conftest import create_user, logged_in_client
from .test_deliverables import _new_project

DLV = [{"code": f"D{i}", "title": t} for i, t in enumerate(
    ["启动包", "调研包", "现状评估", "差距分析", "目标设计", "制度文件包", "路线图", "结项"], 1)]


def _card(**over) -> dict:
    base = {
        "goal": "建立安全运营体系，摸清现状并给出整改优先级",
        "in_scope": "总部安全运营中心", "out_of_scope": "测评、渗透测试、设备采购",
        "deliverables": "D1、D2、D3、D4、D5、D6、D7、D8",
        "acceptance": "按 D1 清单逐项提交，客户书面签收即验收",
        "period": "2027-01-04 至 2027-03-29", "milestones": "启动、调研、分析、设计、验收",
        "payment": "首付 30%；D4 签收后 40%；终验后 30%",
        "tier": "标准档", "retention": "按告知模板默认保留期", "kb_consent": "同意",
        "authorizations": "附件一、附件三随合同签署", "client_requirements": "无", "confidentiality": "已签保密协议",
        "roles": "信息科技部负责人对接", "survey_scope": "资料审阅加访谈", "industry": "城市商业银行",
    }
    base.update(over)
    return {k: ({"value": v, "status": "已核对"} if isinstance(v, str) else v) for k, v in base.items()}


def _run(card: dict, tier: str = "standard") -> dict:
    return cc.check(card, product="SOC", tier=tier, deliverables=DLV)


def test_clean_card_shows_only_one_passed_line():
    res = _run(_card())
    assert res["exceptions"] == [] and not res["blocked"]
    assert res["note"] == cc.PASSED_NOTE and res["passed"] == len(cc.rules()["checks"])


def test_qualification_wording_blocks_save():
    res = _run(_card(goal="协助客户通过等保测评并出具测评结论"))
    assert res["blocked"]
    blocks = [h for h in res["exceptions"] if h["action"] == "block"]
    assert blocks and blocks[0]["check"] == "K-01" and blocks[0]["field"] == "goal"


@pytest.mark.parametrize("over, check, action", [
    ({"acceptance": "以客户满意为准"}, "K-02", "clarify"),
    ({"payment": "合同签订后一次性支付"}, "K-03", "pm"),
    ({"period": "4 周"}, "K-04", "pm"),
    ({"deliverables": "D1、D2、D4"}, "K-05", "clarify"),
    ({"industry": "国有大行"}, "K-06", "clarify"),
    ({"retention": "资料永久保存"}, "K-07", "pm"),
    ({"goal": {"value": "", "status": "合同未写明"}}, "K-08", "clarify"),
    ({"payment": {"value": "", "status": "待澄清"}}, "K-08", "pm"),     # pm_only 字段不出题
])
def test_each_rule_reports_only_its_exception(over, check, action):
    res = _run(_card(**over))
    assert [(h["check"], h["action"]) for h in res["exceptions"]] == [(check, action)]
    assert not res["blocked"] and res["note"] == cc.PASSED_NOTE


def test_tier_signal_only_for_standard_tier():
    assert _run(_card(industry="国有大行"), tier="strict")["exceptions"] == []


def test_questions_use_product_bank_then_generic_and_sort_by_impact():
    card = _card(goal={"value": "", "status": "合同未写明"}, kb_consent={"value": "", "status": "待澄清"},
                 acceptance="以客户满意为准")
    qs = cc.clarifications(_run(card), product="SOC")
    by_field = {q["field"]: q for q in qs}
    assert by_field["goal"]["id"] == "SOC-Q02"                 # 产品题优先
    assert by_field["kb_consent"]["id"] == "Q-09"              # 产品没有同字段题 → 通用题
    assert "以客户满意为准" not in by_field["acceptance"]["ask"] or "满意" in by_field["acceptance"]["ask"]
    for q in qs:
        assert 2 <= len(q["options"]) <= 3 and sum(o["recommended"] for o in q["options"]) == 1
    impacts = [len(q["affects"]) for q in qs]
    assert impacts == sorted(impacts, reverse=True) and [q["no"] for q in qs] == list(range(1, len(qs) + 1))


def test_questions_merge_duplicates_and_drop_qualification_options(monkeypatch):
    data = json.loads(json.dumps(cc.rules()))
    data["questions"] = [{"id": "Q-X", "field": "goal", "ask": "目标？", "affects": ["D1"], "gate": "启动",
                          "options": [{"text": "出具认证结论"}, {"text": "差距分析", "recommended": True},
                                      {"text": "符合性初判"}]}]
    monkeypatch.setattr(cc, "rules", lambda: data)
    monkeypatch.setattr(cc, "product_rules", lambda p: {})
    res = cc.check(_card(goal={"value": "", "status": "合同未写明"}), product="X", tier="standard")
    res["exceptions"].append(dict(res["exceptions"][0]))     # 同字段两条例外只出一题
    qs = cc.clarifications(res, product="X")
    assert len(qs) == 1 and [o["text"] for o in qs[0]["options"]] == ["差距分析", "符合性初判"]


def test_backfill_classifies_answers_and_drafts_change_orders():
    qs = cc.clarifications(_run(_card(goal={"value": "", "status": "合同未写明"},
                                      kb_consent={"value": "", "status": "待澄清"},
                                      retention={"value": "", "status": "待澄清"},
                                      roles={"value": "", "status": "合同未写明"})), product="SOC")
    ids = {q["field"]: q["id"] for q in qs}
    out = cc.backfill(qs, [
        {"question": ids["goal"], "option": 1, "clear": True},
        {"question": ids["kb_consent"], "option": 2, "clear": False, "text": "再看看"},
        {"question": ids["roles"], "option": 1, "conflict": True, "reason": "售前纪要写的是分管领导"},
    ], requests=[{"text": "增加一次分支机构现场调研，费用另议", "deliverables": ["d3", "D9"], "weeks": 2},
                 {"text": "按合同范围做", "in_contract": True}],
        deliverables=DLV, gates_due={"验收"})
    assert [r["field"] for r in out["filled"]] == ["goal"] and out["filled"][0]["answer"] == "自建核心团队加外包 7×24 值守"
    assert {r["field"] for r in out["confirm"]} == {"kb_consent", "roles"}
    assert [r["field"] for r in out["defaulted"]] == ["retention"] and out["defaulted"][0]["mark"] == cc.DEFAULT_MARK
    assert out["pending"] == []
    [co] = out["change_orders"]
    assert co["affects"] == ["D3"] and co["components"] == ["现状评估"] and co["weeks"] == 2
    assert co["commercial"] and co["status"] == "draft"


def test_docx_lists_questions_with_recommended(proj_client):
    code, _ = proj_client
    with SessionLocal() as db:
        from app.models import Project
        p = db.scalar(select(Project).where(Project.code == code))
        qs = cc.clarifications(_run(_card(goal={"value": "", "status": "合同未写明"})), product="SOC")
        data = cc.clarification_docx(p, qs)
    text = "\n".join(x.text for x in Document(io.BytesIO(data)).paragraphs)
    assert "需求澄清清单" in text and "推荐" in text and qs[0]["ask"] in text


# ---- 接口 ----

@pytest.fixture()
def proj_client():
    code = _new_project()
    return code, logged_in_client(app, create_user(("consultant",), {code: "member"}))


def test_api_check_and_docx(proj_client):
    code, c = proj_client
    r = c.post(f"/api/projects/{code}/contract/check", json={"card": _card(goal={"value": "", "status": "合同未写明"},
                                                                             payment="一次性支付")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert not body["blocked"] and body["questions"][0]["field"] == "goal"
    assert [t["check"] for t in body["todo"]] == ["K-03"] and body["note"] == cc.PASSED_NOTE
    r = c.post(f"/api/projects/{code}/contract/clarifications.docx", json={"card": _card(goal="")})
    assert r.status_code == 200 and r.content[:2] == b"PK"
    with SessionLocal() as db:
        a = db.scalars(select(AuditLog).where(AuditLog.action == "contract.clarify_export",
                                              AuditLog.project_code == code)).one()
        assert "建立安全运营体系" not in (a.detail or "")
    assert c.post(f"/api/projects/{code}/contract/clarifications.docx", json={"card": _card()}).status_code == 422


def test_api_answers_blocks_and_fills(proj_client):
    code, c = proj_client
    bad = c.post(f"/api/projects/{code}/contract/answers", json={"card": _card(goal="出具密评结论")})
    assert bad.status_code == 422
    card = _card(goal={"value": "", "status": "合同未写明"})
    qid = c.post(f"/api/projects/{code}/contract/check", json={"card": card}).json()["questions"][0]["id"]
    r = c.post(f"/api/projects/{code}/contract/answers", json={"card": card, "answers": [{"question": qid, "option": 1}]})
    assert r.status_code == 200 and r.json()["filled"][0]["source"] == "会上勾选"


@pytest.fixture()
def fake_store(monkeypatch):
    objs: dict[str, bytes] = {}
    monkeypatch.setattr(storage, "enabled", lambda: True)
    monkeypatch.setattr(storage, "put", lambda area, key, data, ct="": objs.__setitem__(f"{area}:{key}", data))
    monkeypatch.setattr(storage, "get", lambda area, key: objs[f"{area}:{key}"])
    monkeypatch.setattr(storage, "delete", lambda area, key: objs.pop(f"{area}:{key}", None))
    return objs


def test_minutes_import_runs_agent_and_deletes_text(proj_client, fake_store, monkeypatch):
    code, c = proj_client
    card = _card(goal={"value": "", "status": "合同未写明"})
    qid = c.post(f"/api/projects/{code}/contract/check", json={"card": card}).json()["questions"][0]["id"]
    seen: list[str] = []

    def fake(db, agent, message, **kw):
        seen.append(message)
        assert agent == "delivery-pm" and kw["project"] is not None
        out = {"answers": [{"question": qid, "option": 1, "clear": True}],
               "requests": [{"text": "加一份运营手册", "deliverables": ["D6"], "in_contract": False}]}
        return call.Result(gateway.Completion(text="{}", model="m", demo=False), out, "v", "standard")
    monkeypatch.setattr(cc.call, "call", fake)
    minutes = "启动会纪要（虚拟）：客户选择自建核心团队加外包值守；另提出加一份运营手册。"
    r = c.post(f"/api/projects/{code}/contract/minutes", data={"card": json.dumps(card)},
               files={"file": ("纪要.md", minutes.encode(), "text/markdown")})
    assert r.status_code == 202, r.text
    with SessionLocal() as db:
        job = db.scalars(select(Job).where(Job.kind == cc.JOB_KIND, Job.project_code == code)).one()
        jobs.run(db, job)
    assert "自建核心团队" in seen[0]
    assert not any(k.endswith("input.json") for k in fake_store)          # 纪要正文不留存
    st = c.get(f"/api/projects/{code}/contract/minutes").json()
    assert st["status"] == "done" and st["result"]["filled"][0]["question"] == qid
    assert st["result"]["change_orders"][0]["affects"] == ["D6"]
    with SessionLocal() as db:
        for a in db.scalars(select(AuditLog).where(AuditLog.action == cc.ACTION, AuditLog.project_code == code)):
            assert "自建核心团队" not in (a.detail or "")


def test_minutes_refused_for_strict_tier(fake_store):
    code = _new_project("strict")
    c = logged_in_client(app, create_user(("consultant",), {code: "member"}))
    card = _card(goal={"value": "", "status": "合同未写明"})
    r = c.post(f"/api/projects/{code}/contract/minutes", data={"card": json.dumps(card)},
               files={"file": ("纪要.md", "虚拟纪要".encode(), "text/markdown")})
    assert r.status_code == 422
