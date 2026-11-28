"""四期 A32：立项与合同要点卡（上传依据 → 核对要点卡 → 授权与成员）、字段状态、授权台账与关口 1 授权条件、先行启动。"""
import secrets
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.agents import call
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.permissions import PROJECT_LEAD
from app.main import app
from app.models import AuditLog, Confirmation, ContractCard, Job, Project
from app.project import intake, stages
from app.services import gateway, jobs

from .conftest import create_user, logged_in_client
from .test_form_import import fake_store  # noqa: F401  fixture

CONTRACT = "服务合同（虚拟）\n甲方：BANK-A\n服务内容：安全运营现状评估、运营体系设计。\n服务期限：2027-01-05 至 2027-03-31。\n"
OUT = {"goal": "建立安全运营体系", "scope": ["安全运营现状评估", "运营体系设计"], "acceptance": "D4、D5 经甲方书面确认",
       "period": "2027-01-05 至 2027-03-31", "unknown": "丢弃"}


@pytest.fixture()
def agent(monkeypatch):
    box = {"out": dict(OUT)}

    def fake(db, code, message, **kw):
        assert code == "delivery-pm" and "依据正文" in message
        return call.Result(gateway.Completion(text="{}", model="m", demo=False), box["out"], "v", "standard")
    monkeypatch.setattr(intake.call, "call", fake)
    return box


def _new(lead, tier="standard", early=False) -> str:
    code = f"IN-{secrets.token_hex(3).upper()}"
    r = lead.post("/api/projects", json={"code": code, "tier": tier, "product": "SOC", "industry": "银行/金融",
                                         "intake": True, "early_start": early})
    assert r.status_code == 201, r.text
    return code


@pytest.fixture()
def lead():
    c = logged_in_client(app, create_user((PROJECT_LEAD,)))
    yield c
    c.close()


def _run_job() -> None:
    with SessionLocal() as db:
        job = db.scalars(select(Job).where(Job.kind == intake.JOB_KIND).order_by(Job.id.desc())).first()
        jobs.run(db, job)


def _upload(c, code, kind="contract", text=CONTRACT):
    return c.post(f"/api/projects/{code}/intake/basis", data={"basis_kind": kind},
                  files={"file": ("依据.txt", text.encode(), "text/plain")})


def _all_states(card: dict, default="unstated") -> dict:
    return {k: {"value": v["value"], "state": "verified" if v["value"] else default}
            for k, v in card["fields"].items()}


def _sign_auths(c, code, kinds=("annex3", "annex1")):
    for k in kinds:
        r = c.post(f"/api/projects/{code}/intake/authorizations",
                   json={"kind": k, "status": "signed", "version": "v1", "signed_on": date.today().isoformat()})
        assert r.status_code == 200, r.text
    return r.json()


def test_three_steps_contract_card_and_gate1(lead, fake_store, agent):  # noqa: F811
    code = _new(lead)
    st = lead.get(f"/api/projects/{code}/intake").json()
    assert st["intake"] and st["card"] is None
    assert st["gate1"]["missing"] == ["合同要点卡未核对", "附件三合同条款未签", "附件一（标准档）未签"]
    assert "附件三合同条款" in st["data_refusal"]
    # 第一步：上传合同 → 后台抽要点卡，正文不留存
    r = _upload(lead, code)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "extracting" and r.json()["provisional"] is False
    _run_job()
    assert not any(k.endswith("basis.txt") for k in fake_store)
    card = lead.get(f"/api/projects/{code}/intake").json()["card"]
    assert card["status"] == "draft" and card["version"] == 1
    assert card["fields"]["scope"]["value"] == "安全运营现状评估\n运营体系设计"
    assert card["fields"]["scope"]["source"] == "合同" and card["fields"]["scope"]["state"] == ""
    assert card["fields"]["payments"] == {"value": "", "state": "unstated", "source": ""}
    assert "unknown" not in card["fields"]
    # 第二步：没标状态的不能核对完成；资质边界用语不可接受
    put = lambda fields, checked=True: lead.put(f"/api/projects/{code}/intake/card",  # noqa: E731
                                                json={"fields": fields, "checked": checked})
    r = put({k: {"value": v["value"], "state": ""} for k, v in card["fields"].items()})
    assert r.status_code == 422 and "还没标状态" in r.json()["detail"]
    bad = _all_states(card)
    bad["acceptance"] = {"value": "出具等保测评结论", "state": "verified"}
    r = put(bad)
    assert r.status_code == 422 and "不可接受" in r.json()["detail"]
    fields = _all_states(card)
    fields["payments"] = {"value": "", "state": "clarify"}
    r = put(fields)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "checked" and r.json()["fields"]["payments"]["state"] == "clarify"
    assert put(fields).status_code == 409                                  # 已核对的卡不改
    # 第三步：授权台账
    st = _sign_auths(lead, code)
    assert st["gate1"]["missing"] == [] and st["data_refusal"] == ""
    assert {x["kind"]: (x["current"] or {}).get("status") for x in st["ledger"]}["annex1"] == "signed"
    with SessionLocal() as db:
        actions = [a.action for a in db.scalars(select(AuditLog).where(AuditLog.project_code == code))]
        details = " ".join(a.detail or "" for a in db.scalars(select(AuditLog).where(AuditLog.project_code == code)))
    assert "project.contract_card.extract" in actions and "project.authorization.record" in actions
    assert "安全运营现状评估" not in details and "BANK-A" not in details          # 审计不记原文


def test_data_refused_until_authorized(lead):
    code = _new(lead)
    note = {"kind": "interview", "title": "访谈", "content": "SOC 值班情况", "interviewee_role": "运维主管"}
    r = lead.post(f"/api/projects/{code}/notes", json=note)
    assert r.status_code == 422 and "授权台账" in r.json()["detail"]
    _sign_auths(lead, code)
    assert lead.post(f"/api/projects/{code}/notes", json=note).status_code == 201
    # 附件一撤回后又拒收
    lead.post(f"/api/projects/{code}/intake/authorizations",
              json={"kind": "annex1", "status": "withdrawn", "signed_on": date.today().isoformat()})
    assert lead.post(f"/api/projects/{code}/notes", json=note).status_code == 422
    st = lead.get(f"/api/projects/{code}/intake").json()
    assert len([h for h in st["history"] if h["kind"] == "annex1"]) == 2       # 变更留历史


def test_legacy_projects_unaffected(lead):
    code = f"LG-{secrets.token_hex(3).upper()}"
    assert lead.post("/api/projects", json={"code": code, "tier": "standard", "product": "SOC",
                                            "industry": "电力"}).status_code == 201
    st = lead.get(f"/api/projects/{code}/intake").json()
    assert not st["intake"] and st["gate1"]["missing"] == [] and st["data_refusal"] == ""
    note = {"kind": "interview", "title": "访谈", "content": "SOC 值班情况", "interviewee_role": "运维主管"}
    assert lead.post(f"/api/projects/{code}/notes", json=note).status_code == 201


def test_strict_manual_card_and_annex2(lead):
    code = _new(lead, tier="strict")
    st = lead.get(f"/api/projects/{code}/intake").json()
    assert st["gate1"]["missing"][1:] == ["附件三合同条款未签", "附件二（严格档）未签"]
    assert {x["kind"] for x in st["ledger"]} >= {"annex2", "deid_consent"} and "annex1" not in {x["kind"] for x in st["ledger"]}
    assert _upload(lead, code).status_code == 422                          # 严格档不上传合同
    fields = {k: {"value": "", "state": "unstated"} for k in intake.FIELD_KEYS}
    fields["goal"] = {"value": "建立安全运营体系", "state": "verified"}
    r = lead.put(f"/api/projects/{code}/intake/card", json={"fields": fields, "checked": True})
    assert r.status_code == 200 and r.json()["basis_kind"] == "manual"
    assert r.json()["fields"]["goal"]["source"] == "项目经理填写"
    r = lead.post(f"/api/projects/{code}/intake/authorizations",
                  json={"kind": "annex1", "signed_on": date.today().isoformat()})
    assert r.status_code == 422
    st = _sign_auths(lead, code, ("annex3", "annex2"))
    assert st["gate1"]["missing"] == []


def test_deid_consent_syncs_flowback(lead):
    code = _new(lead)
    with SessionLocal() as db:
        assert db.scalar(select(Project).where(Project.code == code)).flowback_allowed is False
    _sign_auths(lead, code, ("deid_consent",))
    with SessionLocal() as db:
        assert db.scalar(select(Project).where(Project.code == code)).flowback_allowed is True
    lead.post(f"/api/projects/{code}/intake/authorizations",
              json={"kind": "deid_consent", "status": "withdrawn", "signed_on": date.today().isoformat()})
    with SessionLocal() as db:
        assert db.scalar(select(Project).where(Project.code == code)).flowback_allowed is False


def test_member_not_manager_cannot_write_and_non_member_404(lead):
    code = _new(lead)
    member = logged_in_client(app, create_user(("consultant",), {code: "member"}))
    outsider = logged_in_client(app, create_user(("consultant",)))
    try:
        assert member.get(f"/api/projects/{code}/intake").status_code == 200
        assert member.put(f"/api/projects/{code}/intake/card", json={"fields": {}}).status_code == 403
        assert member.post(f"/api/projects/{code}/intake/authorizations",
                           json={"kind": "annex3", "signed_on": date.today().isoformat()}).status_code == 403
        assert outsider.get(f"/api/projects/{code}/intake").status_code == 404
        assert outsider.put(f"/api/projects/{code}/intake/card", json={"fields": {}}).status_code == 404
    finally:
        member.close()
        outsider.close()


def test_early_start_flow(lead, fake_store, agent, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "auto_flow", True)
    code = _new(lead, early=True)
    st = lead.get(f"/api/projects/{code}/intake").json()
    assert st["early_start"]["days"] == 1 and not st["early_start"]["approved"]
    assert "先行启动待部门负责人确认" in st["gate1"]["missing"]
    # 依据：中标通知书 → 临时卡；不是先行启动的项目不能传非合同依据
    r = _upload(lead, code, kind="award", text="中标通知书（虚拟）\nBANK-A 安全运营体系设计项目\n")
    assert r.status_code == 202 and r.json()["provisional"] is True
    _run_job()
    card = lead.get(f"/api/projects/{code}/intake").json()["card"]
    assert card["fields"]["goal"]["source"] == "中标通知书"
    assert lead.put(f"/api/projects/{code}/intake/card",
                    json={"fields": _all_states(card), "checked": True}).status_code == 200
    _sign_auths(lead, code)
    # 项目经理不能自己确认；其他部门负责人可以（不必是成员）
    assert lead.post(f"/api/projects/{code}/early-start/approve").status_code == 403
    consultant = logged_in_client(app, create_user(("consultant",), {code: "member"}))
    other = logged_in_client(app, create_user((PROJECT_LEAD,)))
    try:
        assert consultant.post(f"/api/projects/{code}/early-start/approve").status_code == 403
        assert code in [x["code"] for x in other.get("/api/early-starts").json()]
        r = other.post(f"/api/projects/{code}/early-start/approve")
        assert r.status_code == 200 and r.json()["approved"]
        assert other.post(f"/api/projects/{code}/early-start/approve").status_code == 409
        assert consultant.get("/api/early-starts").status_code == 403
    finally:
        consultant.close()
    st = lead.get(f"/api/projects/{code}/intake").json()
    assert st["gate1"]["missing"] == []
    # 合同未签：不能登记验收（结项），结项清单不 ready
    r = lead.post(f"/api/projects/{code}/acceptance", json={"accepted_on": date.today().isoformat()})
    assert r.status_code == 409 and "合同未签" in r.json()["detail"]
    assert lead.get(f"/api/projects/{code}/closeout").json()["contract"]
    # 满 30 天未签：overdue
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        p.early_start_on = date.today() - timedelta(days=40)
        db.commit()
        assert intake.early_start(db, p)["overdue"] is True
    assert code in [x["code"] for x in other.get("/api/early-starts").json()]
    # 合同到达：范围变了 → 待澄清 + 范围确认卡；其余一致的自动已核对
    agent["out"] = {**OUT, "scope": ["安全运营现状评估"]}
    assert _upload(lead, code).status_code == 202
    _run_job()
    card = lead.get(f"/api/projects/{code}/intake").json()["card"]
    assert card["version"] == 2 and card["provisional"] is False and card["status"] == "draft"
    assert card["fields"]["scope"]["state"] == "clarify" and card["fields"]["goal"]["state"] == "verified"
    with SessionLocal() as db:
        titles = [c.title for c in db.scalars(select(Confirmation).where(
            Confirmation.project_id == select(Project.id).where(Project.code == code).scalar_subquery()))]
    assert "合同与先行启动要点卡不一致：范围" in titles
    card["fields"]["scope"]["state"] = "verified"
    assert lead.put(f"/api/projects/{code}/intake/card",
                    json={"fields": card["fields"], "checked": True}).status_code == 200
    st = lead.get(f"/api/projects/{code}/intake").json()
    assert st["early_start"]["contract_signed"] and [v["version"] for v in st["versions"]] == [2, 1]
    assert code not in [x["code"] for x in other.get("/api/early-starts").json()]
    other.close()


def test_contract_arrival_identical_auto_checks(lead, fake_store, agent):  # noqa: F811
    code = _new(lead, early=True)
    assert _upload(lead, code, kind="email", text="客户确认范围的邮件（虚拟）\n").status_code == 202
    _run_job()
    card = lead.get(f"/api/projects/{code}/intake").json()["card"]
    lead.put(f"/api/projects/{code}/intake/card", json={"fields": _all_states(card), "checked": True})
    assert _upload(lead, code).status_code == 202
    _run_job()
    card = lead.get(f"/api/projects/{code}/intake").json()["card"]
    assert card["status"] == "checked" and card["checked_by"] == "platform"


def test_non_contract_basis_needs_early_start(lead, fake_store):  # noqa: F811
    code = _new(lead)
    r = _upload(lead, code, kind="award")
    assert r.status_code == 422 and "先行启动" in r.json()["detail"]


def test_stage_stays_in_launch_until_gate1(lead, monkeypatch):
    monkeypatch.setattr(get_settings(), "auto_flow", True)
    code = _new(lead)
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        for d in p.deliverables:
            if d.code == "D1":
                d.status = "signed"
        db.commit()
        assert stages.scan(db, p) == [] and p.phase == 0               # 要点卡与授权未齐：不出“启动”
        db.add(ContractCard(project_id=p.id, version=1, basis_kind="manual", status="checked", fields={},
                            created_by="t"))
        db.commit()
    _sign_auths(lead, code)
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        assert stages.scan(db, p) == [code]


def test_deletion_purges_cards(lead):
    from app.project import deletion

    code = _new(lead)
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        db.add(ContractCard(project_id=p.id, version=1, basis_kind="manual", status="draft", fields={}, created_by="t"))
        db.commit()
        counts = deletion.purge(db, p)
        db.commit()
        assert counts["contract_cards"] == 1
        assert db.scalar(select(ContractCard).where(ContractCard.project_id == p.id)) is None
