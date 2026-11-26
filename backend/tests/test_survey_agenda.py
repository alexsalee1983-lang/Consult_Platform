"""四期 E3③：现场议程与证据不足自动分流（app/project/agenda.py）。

题库的受访角色、控制域、获取渠道、可预填资料来源随题入库（不进调研包）；议程按受访角色分“快速确认 → 议程 → 补问”三段：
已有自评 / 资料答案待确认的进快速确认，矛盾、没有答案、开放讨论进议程，现场题卡渠道的进补问；
证据不足有可预填资料的同时列补资料请求；相关角色都访谈过仍没答案、仍矛盾的转人工发确认卡（不重发）；
没写受访角色的题按产品 inputs.personas 关注域匹配；审计不记题面和答案原文；非成员 404。"""
import secrets

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Confirmation, Project
from app.models.survey import SurveyQuestion
from app.project import agenda, survey

from .conftest import create_user, logged_in_client

TAG = "".join(secrets.choice("ABCDEFGHJKLMNP") for _ in range(4))
QA, QB, QC, QD, QE, QF, QG = (f"SOC-G{TAG}-00{i}" for i in range(1, 8))
OPTS = [{"key": "A", "level": 1, "text": "没有书面规定"}, {"key": "B", "level": 3, "text": "有书面规定并执行"},
        {"key": "X", "level": None, "text": "不清楚"}]


def _q(qid: str, roles: list[str], channel: str, kind: str = "closed", prefill: tuple = (), **kw) -> dict:
    q = {"id": qid, "product": "SOC", "domain": "流程", "control_ids": ["SOC.PRC.01"], "kind": kind,
         "stem": f"题面 {qid[-3:]}", "respondent_roles": roles, "channel": channel,
         "prefill": [{"source": s} for s in prefill], **kw}
    if kind == "closed":
        q["options"] = OPTS
    return q


BANK = [
    _q(QA, ["安全负责人"], "self"),                                   # 自评答了 → 快速确认
    _q(QB, ["运营值班"], "self"),                                     # 自评与资料矛盾 → 议程现场定案
    _q(QC, ["运营值班"], "prefill", prefill=("siem_stats",)),         # 没答案、可预填 → 补资料请求 + 议程
    _q(QD, ["IT运维"], "onsite"),                                     # 现场题卡、没答案 → 补问
    _q(QE, ["安全负责人"], "discussion", kind="open"),                # 开放讨论 → 议程
    _q(QF, ["安全负责人"], "self"),                                   # 没答案、不可预填 → 议程；访谈后仍没 → 转人工
    _q(QG, [], "self", domain="度量与改进"),                         # 没写角色 → 按 personas 关注域
]


@pytest.fixture(scope="module", autouse=True)
def _bank():
    with SessionLocal() as db:
        survey.import_bank(db, BANK)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(get_settings(), "survey_spot_rate", 0.0)
    monkeypatch.setattr(agenda, "personas", lambda p: (("安全负责人", ("流程",)), ("管理层", ("度量与改进",))))


def _setup(tier: str = "standard"):
    code = f"AG-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        db.add(Project(code=code, service="SOC 运营体系设计", industry="银行", tier=tier))
        db.commit()
    c = logged_in_client(app, create_user(("consultant",), {code: "manager"}))
    pack = c.post(f"/api/projects/{code}/survey/packs").json()
    return code, c, f"/api/projects/{code}/survey/packs/{pack['code']}/results", pack


def _send(c, url, pack, channel, role, answers):
    r = c.post(url, json={"pack_hash": pack["pack_hash"], "channel": channel,
                          "responses": [{"respondent_role": role, "answers": answers}]})
    assert r.status_code == 201, r.text
    return r.json()


def _agenda(c, code) -> dict:
    r = c.get(f"/api/projects/{code}/survey/agenda")
    assert r.status_code == 200, r.text
    return r.json()


def _sec(plan: dict, role: str, section: str) -> dict:
    r = next((x for x in plan["roles"] if x["role"] == role), None)
    return {i["qid"]: i for i in (r[section] if r else []) if f"-G{TAG}-" in i["qid"]}


def test_meta_stored_not_in_pack():
    with SessionLocal() as db:
        q = db.scalar(select(SurveyQuestion).where(SurveyQuestion.qid == QC))
        assert survey.question_meta(q) == {"roles": ["运营值班"], "domain": "流程", "channel": "prefill",
                                           "prefill": ["siem_stats"]}
    _, _, _, pack = _setup()
    mine = [q for q in pack["questions"] if f"-G{TAG}-" in q["qid"]]
    assert len(mine) == 7 and all(set(q) <= {"qid", "control", "kind", "text", "options"} for q in mine)
    assert "_meta" not in str(pack) and "siem_stats" not in str(pack)


def test_bank_channel_validated():
    with SessionLocal() as db, pytest.raises(survey.SurveyError) as e:
        survey.import_bank(db, [_q(f"SOC-G{TAG}-099", [], "fax")])
    assert "获取渠道" in e.value.detail


def test_agenda_three_sections_by_role_and_requests():
    code, c, url, pack = _setup()
    _send(c, url, pack, "self_assess", "安全负责人", [{"qid": QA, "value": "B"}])
    _send(c, url, pack, "self_assess", "运营值班", [{"qid": QB, "value": "A"}])
    _send(c, url, pack, "prefill", "客户资料", [{"qid": QB, "value": "B"}])
    plan = _agenda(c, code)
    quick = _sec(plan, "安全负责人", "quick_confirm")
    assert list(quick) == [QA] and quick[QA]["answers"][0]["label"] == "有书面规定并执行"
    assert set(_sec(plan, "安全负责人", "agenda")) == {QE, QF}
    assert _sec(plan, "安全负责人", "agenda")[QE]["reason"] == "open"
    duty = _sec(plan, "运营值班", "agenda")
    assert duty[QB]["reason"] == "conflict" and {a["value"] for a in duty[QB]["answers"]} == {"A", "B"}
    assert duty[QC]["reason"] == "insufficient"
    assert _sec(plan, "IT运维", "followup")[QD]["reason"] == "observe"
    assert QG in _sec(plan, "管理层", "agenda")                               # 没写角色按 personas 关注域
    req = {i["qid"]: i for i in plan["requests"] if f"-G{TAG}-" in i["qid"]}
    assert list(req) == [QC] and req[QC]["materials"] == ["siem_stats"]
    assert not [i for i in plan["manual"] if f"-G{TAG}-" in i["qid"]]
    assert all("text" in i for i in quick.values())


def test_after_interview_unresolved_goes_to_card_once():
    code, c, url, pack = _setup()
    _send(c, url, pack, "self_assess", "运营值班", [{"qid": QB, "value": "A"}])
    _send(c, url, pack, "prefill", "客户资料", [{"qid": QB, "value": "B"}])
    out = _send(c, url, pack, "onsite", "安全负责人", [{"qid": QA, "value": "B"}])
    mine = lambda plan: {i["qid"]: i for i in plan["manual"] if f"-G{TAG}-" in i["qid"]}   # noqa: E731
    plan = _agenda(c, code)
    assert QF in mine(plan) and mine(plan)[QF]["card_kind"] == "证据不足" and mine(plan)[QF]["card_id"]
    assert QA not in _sec(plan, "安全负责人", "quick_confirm")              # 访谈过，单条口述留事实确认单
    assert QE not in mine(plan)                                             # 开放题访谈后不出卡
    assert out["to_manual"] >= 1
    _send(c, url, pack, "onsite", "运营值班", [{"qid": QB, "value": "A"}])
    plan = _agenda(c, code)
    assert mine(plan)[QB]["card_kind"] == "来源冲突"
    assert QC not in mine(plan) and QC in {i["qid"] for i in plan["requests"]}   # 可预填 → 只留补资料请求
    _send(c, url, pack, "onsite", "运营值班", [{"qid": QB, "value": "A"}])      # 再导入不重发卡
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        cards = list(db.scalars(select(Confirmation).where(Confirmation.project_id == p.id)))
        titles = [x.title for x in cards]
        assert titles.count(f"调研证据不足 · {QF}（SOC.PRC.01）") == 1
        assert titles.count(f"调研来源冲突 · {QB}（SOC.PRC.01）") == 1
        assert all(x.agent_code == "research-interview" and "题面" not in x.title for x in cards)
        logs = list(db.scalars(select(AuditLog).where(AuditLog.action == "survey.route",
                                                      AuditLog.project_code == code)))
        assert logs and all("题面" not in str(x.detail) and "书面规定" not in str(x.detail) for x in logs)


def test_confirmed_questions_leave_agenda():
    code, c, url, pack = _setup()
    _send(c, url, pack, "self_assess", "安全负责人", [{"qid": QA, "value": "B"}])
    fact = next(f for f in c.get(f"/api/projects/{code}/survey/facts").json() if f["qid"] == QA)
    assert c.post(f"/api/projects/{code}/survey/facts/{fact['id']}/confirm",
                  json={"confirmed_role": "安全负责人"}).status_code == 200
    assert QA not in _sec(_agenda(c, code), "安全负责人", "quick_confirm")


def test_strict_tier_agenda_and_non_member():
    code, c, _, _ = _setup("strict")
    assert _agenda(c, code)["counts"]["questions"] >= 7
    other = logged_in_client(app, create_user(("consultant",), {}))
    assert other.get(f"/api/projects/{code}/survey/agenda").status_code == 404
