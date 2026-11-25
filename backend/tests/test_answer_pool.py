"""四期 E3①：答案池（app/project/survey.py，迁移 0057）。

按题号汇聚答案，每条记来源渠道和可信度；自动确认只在“非关键控制项、无矛盾、有高可信来源（客户资料或现场证据）”时发生；
只有客户自评或现场口述的、矛盾的、关键控制项的都留给人；自动确认后来了矛盾答案就退回人工；
自动确认按比例进抽检队列，抽检不通过退回人工；自动确认和抽检结论只记审计计数与编号，不记原文。"""
import secrets

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Project
from app.project import survey

from .conftest import create_user, logged_in_client

TAG = "".join(secrets.choice("ABCDEFGHJKLMNP") for _ in range(4))
Q1, Q2, QK = f"SOC-P{TAG}-001", f"SOC-P{TAG}-002", f"SOC-P{TAG}-003"
OPTS = [{"key": "A", "level": 1, "text": "没有分级表"}, {"key": "B", "level": 3, "text": "有分级表且工单计时"},
        {"key": "X", "level": None, "text": "不清楚"}]
BANK = [
    {"id": Q1, "product": "SOC", "control_ids": ["SOC.PRC.01"], "kind": "closed", "stem": "告警是否分级？", "options": OPTS},
    {"id": Q2, "product": "SOC", "control_ids": ["SOC.ORG.02"], "kind": "closed", "stem": "交接是否有记录？", "options": OPTS},
    {"id": QK, "product": "SOC", "control_ids": ["SOC.TEC.01"], "kind": "closed", "stem": "日志是否全量接入？",
     "options": OPTS, "key_control": True},
]


@pytest.fixture(scope="module", autouse=True)
def _bank():
    with SessionLocal() as db:
        survey.import_bank(db, BANK)


@pytest.fixture(autouse=True)
def _no_spot(monkeypatch):
    monkeypatch.setattr(get_settings(), "survey_spot_rate", 0.0)


def _setup() -> tuple[str, object, str, str]:
    code = f"AP-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        db.add(Project(code=code, service="SOC 运营体系设计", industry="银行", tier="standard"))
        db.commit()
    c = logged_in_client(app, create_user(("consultant",), {code: "manager"}))
    pack = c.post(f"/api/projects/{code}/survey/packs").json()
    return code, c, f"/api/projects/{code}/survey/packs/{pack['code']}/results", pack["pack_hash"]


def _send(c, url, h, channel, role, answers, status=201):
    r = c.post(url, json={"pack_hash": h, "channel": channel,
                          "responses": [{"respondent_role": role, "answers": answers}]})
    assert r.status_code == status, r.text
    return r.json()


def _pool(c, code) -> dict:
    r = c.get(f"/api/projects/{code}/survey/pool")
    assert r.status_code == 200, r.text
    return {q["qid"]: q for q in r.json()}


def test_single_document_source_auto_confirms_low_sources_wait():
    code, c, url, h = _setup()
    out = _send(c, url, h, "prefill", "客户资料", [{"qid": Q1, "value": "B"}])
    assert out["auto_confirmed"] == 1
    _send(c, url, h, "self_assess", "安全运营负责人", [{"qid": Q2, "value": "B"}])
    pool = _pool(c, code)
    assert pool[Q1]["status"] == "auto" and pool[Q1]["value"] == "B"
    a = pool[Q1]["answers"][0]
    assert a["channel"] == "prefill" and a["source"] == "document" and a["credibility"] == 3
    assert pool[Q2]["status"] == "pending"                       # 只有客户自评 → 人工
    _send(c, url, h, "onsite", "值班组长", [{"qid": Q2, "value": "B"}])
    assert _pool(c, code)[Q2]["status"] == "pending"             # 自评 + 口述一致也没有高可信来源 → 人工
    _send(c, url, h, "onsite", "值班组长", [{"qid": Q2, "value": "B", "source": "evidence"}])
    pool = _pool(c, code)
    assert pool[Q2]["status"] == "auto"                          # 现场证据一致 → 自动确认
    assert sum(x["fact_status"] == "auto" for x in pool[Q2]["answers"]) == 1
    with SessionLocal() as db:
        logs = db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                                 AuditLog.action == "survey.auto_confirm")).all()
        assert len(logs) == 2 and all("有分级表" not in (x.detail or "") for x in logs)


def test_key_control_and_conflict_go_to_human():
    code, c, url, h = _setup()
    _send(c, url, h, "onsite", "值班组长", [{"qid": QK, "value": "B", "source": "evidence"}])
    _send(c, url, h, "prefill", "客户资料", [{"qid": Q1, "value": "B"}])
    _send(c, url, h, "self_assess", "安全运营负责人", [{"qid": Q2, "value": "A"}])
    _send(c, url, h, "prefill", "客户资料", [{"qid": Q2, "value": "B"}])
    pool = _pool(c, code)
    assert pool[QK]["status"] == "pending" and pool[QK]["key_control"] is True
    assert pool[Q2]["status"] == "conflict"
    # 自动确认后来了矛盾答案：退回人工、标矛盾
    out = _send(c, url, h, "onsite", "值班组长", [{"qid": Q1, "value": "A"}])
    assert out["conflicts"] >= 1
    pool = _pool(c, code)
    assert pool[Q1]["status"] == "conflict" and not any(x["fact_status"] == "auto" for x in pool[Q1]["answers"])
    # 人工确认后状态为人工确认
    pick = next(x for x in pool[Q1]["answers"] if x["value"] == "A")
    r = c.post(f"/api/projects/{code}/survey/facts/{pick['id']}/confirm", json={"confirmed_role": "信息科技部负责人"})
    assert r.status_code == 200
    assert _pool(c, code)[Q1]["status"] == "confirmed"


@pytest.mark.parametrize("channel,source", [("prefill", "self"), ("self_assess", "evidence"), ("chat", ""),
                                            ("onsite", "document")])
def test_channel_source_validated(channel, source):
    code, c, url, h = _setup()
    _send(c, url, h, channel, "值班组长", [{"qid": Q1, "value": "B", "source": source}], status=422)


def test_spot_check_queue_and_verdicts(monkeypatch):
    monkeypatch.setattr(get_settings(), "survey_spot_rate", 1.0)
    code, c, url, h = _setup()
    _send(c, url, h, "prefill", "客户资料", [{"qid": Q1, "value": "B"}, {"qid": Q2, "value": "A"}])
    queue = c.get(f"/api/projects/{code}/survey/spot-checks").json()
    assert {x["qid"] for x in queue} == {Q1, Q2} and all(x["spot_check"] == "pending" for x in queue)
    ok = next(x for x in queue if x["qid"] == Q1)
    bad = next(x for x in queue if x["qid"] == Q2)
    r = c.post(f"/api/projects/{code}/survey/facts/{ok['id']}/spot-check", json={"passed": True})
    assert r.status_code == 200 and r.json()["spot_check"] == "passed" and r.json()["fact_status"] == "auto"
    r = c.post(f"/api/projects/{code}/survey/facts/{bad['id']}/spot-check", json={"passed": False})
    assert r.status_code == 200 and r.json()["spot_check"] == "failed" and r.json()["fact_status"] == "pending"
    assert c.get(f"/api/projects/{code}/survey/spot-checks").json() == []
    assert c.post(f"/api/projects/{code}/survey/facts/{ok['id']}/spot-check",
                  json={"passed": True}).status_code == 422          # 不在抽检队列
    _send(c, url, h, "prefill", "客户资料", [{"qid": Q2, "value": "A"}])
    pool = _pool(c, code)
    assert pool[Q2]["status"] == "pending"                       # 抽检不通过的题不再自动确认
    with SessionLocal() as db:
        logs = db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                                 AuditLog.action == "survey.spot_check")).all()
        assert sorted(x.result for x in logs) == ["failed", "ok"]


def test_spot_rate_default_and_hook():
    assert type(get_settings()).model_fields["survey_spot_rate"].default == 0.1
    with SessionLocal() as db:
        assert survey.spot_rate(db, None) == get_settings().survey_spot_rate


def test_nonmember_404():
    code, c, url, h = _setup()
    outsider = logged_in_client(app, create_user(("consultant",)))
    assert outsider.get(f"/api/projects/{code}/survey/pool").status_code == 404
    assert outsider.get(f"/api/projects/{code}/survey/spot-checks").status_code == 404
