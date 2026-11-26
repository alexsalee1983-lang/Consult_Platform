"""四期 E2①：调研助手平台侧（app/project/survey.py）。

题库导入校验；按项目画像选题；调研包哈希稳定、严格档不含画像字段；回传核对包哈希和题号；
封闭题直转待确认事实、“不清楚”只计数；跨角色矛盾标出、确认一条取代同题其他条；
开放题过 guard，严格档按摘要卡规则拒收原文、须有有效同意；审计不记原文；删项目清干净。"""
import secrets
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Project
from app.models.survey import SurveyAnswer, SurveyQuestion
from app.project import survey

from .conftest import create_user, logged_in_client

TAG = "".join(secrets.choice("ABCDEFGHJKLMNP") for _ in range(4))   # 题号域段，隔开别的测试
Q1, Q2, Q3, Q4 = (f"SOC-T{TAG}-00{i}" for i in range(1, 5))
UNCLEAR = {"key": "X", "level": None, "text": "不清楚"}
BANK = [
    {"id": Q1, "product": "SOC", "control_ids": ["SOC.PRC.01"], "kind": "closed", "stem": "告警是否按级别规定处置时限？",
     "options": [{"key": "A", "level": 1, "text": "没有分级表"}, {"key": "B", "level": 2, "text": "有分级表但工单不计时"},
                 {"key": "C", "level": 3, "text": "有分级表且工单按级别计时"}, UNCLEAR],
     "industry_variants": [{"industry": "BANK", "stem": "银行版：告警是否按级别规定处置时限？", "followups": []}]},
    {"id": Q2, "product": "SOC", "control_ids": ["SOC.ORG.02"], "kind": "closed", "stem": "夜间值班交接是否有记录？",
     "options": [{"key": "A", "level": 1, "text": "无交接记录"}, {"key": "B", "level": 3, "text": "每班有交接记录"},
                 UNCLEAR]},
    {"id": Q3, "product": "SOC", "control_ids": ["SOC.ORG.02"], "kind": "open", "stem": "交接中最常遗漏的事项是什么？"},
    {"id": Q4, "product": "IRE", "control_ids": ["IRE.INC.01"], "kind": "closed", "stem": "别的产品的题",
     "options": [{"key": "A", "text": "是"}, {"key": "B", "text": "否"}]},
]


@pytest.fixture(scope="module", autouse=True)
def _bank():
    with SessionLocal() as db:
        survey.import_bank(db, BANK)


def _project(tier: str) -> str:
    code = f"SV-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        db.add(Project(code=code, service="SOC 运营体系设计", industry="银行", tier=tier))
        db.commit()
    return code


def _client(code: str):
    return logged_in_client(app, create_user(("consultant",), {code: "manager"}))


def _mine(qids: list[str]) -> list[str]:
    return [q for q in qids if f"-T{TAG}-" in q]


@pytest.mark.parametrize("bad,msg", [
    ({"id": "x", "product": "SOC", "control_ids": ["c"], "kind": "closed", "stem": "t"}, "题号"),
    ({"id": "SOC-BAD-001", "product": "SOC", "control_ids": ["c"], "kind": "closed", "stem": "t",
      "options": [{"key": "A", "text": "是"}, UNCLEAR]}, "至少两个选项"),
    ({"id": "SOC-BAD-002", "product": "SOC", "control_ids": ["c"], "kind": "multi", "stem": "t"}, "题型"),
    ({"id": "SOC-BAD-003", "product": "SOC", "control_ids": ["c"], "kind": "open",
      "stem": "管理员口令为 Passw0rd!2026"}, "机密级"),
    ({"id": "SOC-BAD-004", "product": "SOC", "control_ids": ["c"], "kind": "open", "stem": "t",
      "industry_variants": [{"industry": "MARS", "stem": "t"}]}, "行业"),
])
def test_bank_validation(bad, msg):
    with SessionLocal() as db, pytest.raises(survey.SurveyError) as e:
        survey.import_bank(db, [bad])
    assert msg in e.value.detail


def test_bank_import_upserts():
    with SessionLocal() as db:
        added, updated = survey.import_bank(db, BANK[:1])
        assert (added, updated) == (0, 1)


def test_e1_sample_banks_import():
    """仓库里的 E1 题库样例能整批通过导入校验；评分等级不入库。"""
    root = Path(__file__).resolve().parents[2] / "knowledge" / "question_bank"
    questions = [q for f in sorted(root.glob("*.yaml")) for q in survey.load_bank_file(f)]
    clean = [survey._validate(q) for q in questions]
    assert clean and all("level" not in o for q in clean for o in q["options"])
    assert all(o["key"] != "X" for q in clean for o in q["options"])


def test_selection_by_profile():
    code = _project("standard")
    c = _client(code)
    r = c.get(f"/api/projects/{code}/survey/questions")
    assert r.status_code == 200
    assert _mine([q["qid"] for q in r.json()]) == [Q1, Q2, Q3]
    assert r.json()[[q["qid"] for q in r.json()].index(Q1)]["text"].startswith("银行版")


def test_pack_hash_and_strict_fields():
    std, strict = _project("standard"), _project("strict")
    p1 = _client(std).post(f"/api/projects/{std}/survey/packs").json()
    assert p1["code"] == "P-01" and p1["project"] == std and p1["tier"] == "standard"
    assert p1["profile"] == {"service": "SOC 运营体系设计", "industry": "银行"}
    assert survey.pack_hash({k: v for k, v in p1.items() if k != "pack_hash"}) == p1["pack_hash"]
    p2 = _client(strict).post(f"/api/projects/{strict}/survey/packs").json()
    assert "profile" not in p2 and p2["tier"] == "strict"
    q1 = next(q for q in p2["questions"] if q["qid"] == Q1)
    assert {"key": "X", "label": "不清楚"} in q1["options"] and all("level" not in o for o in q1["options"])


def _answers(role: str, v1: str = "C", v2: str = "B", text: str = "") -> dict:
    ans = [{"qid": Q1, "value": v1}, {"qid": Q2, "value": v2}]
    if text:
        ans.append({"qid": Q3, "text": text})
    return {"respondent_role": role, "answers": ans}


def test_results_to_facts_and_conflicts():
    code = _project("standard")
    c = _client(code)
    pack = c.post(f"/api/projects/{code}/survey/packs").json()
    url = f"/api/projects/{code}/survey/packs/{pack['code']}/results"
    assert c.post(url, json={"pack_hash": "0" * 64, "responses": [_answers("安全运营负责人")]}).status_code == 422
    bad_q = {"respondent_role": "r", "answers": [{"qid": Q4, "value": "A"}]}
    assert c.post(url, json={"pack_hash": pack["pack_hash"], "responses": [bad_q]}).status_code == 422
    bad_v = {"respondent_role": "r", "answers": [{"qid": Q1, "value": "Z"}]}
    assert c.post(url, json={"pack_hash": pack["pack_hash"], "responses": [bad_v]}).status_code == 422
    with SessionLocal() as db:
        before = db.scalar(select(SurveyQuestion.unclear_count).where(SurveyQuestion.qid == Q2))
    r = c.post(url, json={"pack_hash": pack["pack_hash"], "responses": [
        _answers("安全运营负责人", "C", "X", text="交接时漏报未闭环工单"), _answers("值班组长", "A", "B")]})
    assert r.status_code == 201, r.text
    assert r.json() == {"facts": 3, "unclear": 1, "open": 1, "conflicts": 1, "auto_confirmed": 0}
    with SessionLocal() as db:
        assert db.scalar(select(SurveyQuestion.unclear_count).where(SurveyQuestion.qid == Q2)) \
            == before + 1
    facts = c.get(f"/api/projects/{code}/survey/facts").json()
    q1 = [f for f in facts if f["qid"] == Q1]
    assert len(q1) == 2 and all(f["conflict"] for f in q1) and {f["label"] for f in q1} == {"没有分级表", "有分级表且工单按级别计时"}
    pick = next(f for f in q1 if f["value"] == "A")
    r = c.post(f"/api/projects/{code}/survey/facts/{pick['id']}/confirm", json={"confirmed_role": "信息科技部负责人"})
    assert r.status_code == 200 and r.json()["fact_status"] == "confirmed"
    facts = {f["id"]: f for f in c.get(f"/api/projects/{code}/survey/facts").json()}
    other = next(f for f in q1 if f["id"] != pick["id"])
    assert facts[other["id"]]["fact_status"] == "superseded"
    assert not any(f["conflict"] for f in facts.values() if f["qid"] == Q1)
    assert c.post(f"/api/projects/{code}/survey/facts/{pick['id']}/confirm",
                  json={"confirmed_role": "张经理"}).status_code == 422   # 角色里不写姓名
    with SessionLocal() as db:
        logs = db.scalars(select(AuditLog).where(AuditLog.project_code == code)).all()
        assert all("交接时漏报" not in (a.detail or "") for a in logs)


def test_guard_blocks_open_answer():
    code = _project("standard")
    c = _client(code)
    pack = c.post(f"/api/projects/{code}/survey/packs").json()
    r = c.post(f"/api/projects/{code}/survey/packs/{pack['code']}/results",
               json={"pack_hash": pack["pack_hash"],
                     "responses": [_answers("值班组长", text="堡垒机管理员口令为 Passw0rd!2026")]})
    assert r.status_code == 422 and "机密级" in r.text
    with SessionLocal() as db:
        pid = db.scalar(select(Project.id).where(Project.code == code))
        assert not db.scalars(select(SurveyAnswer).where(SurveyAnswer.project_id == pid)).all()


def test_strict_requires_consent_and_card_rules():
    code = _project("strict")
    c = _client(code)
    pack = c.post(f"/api/projects/{code}/survey/packs").json()
    url = f"/api/projects/{code}/survey/packs/{pack['code']}/results"
    body = {"pack_hash": pack["pack_hash"], "responses": [_answers("值班组长")]}
    r = c.post(url, json=body)
    assert r.status_code == 422 and "同意" in r.text
    signed = (date.today() - timedelta(days=1)).isoformat()
    assert c.post(f"/api/projects/{code}/consents", json={"doc_ref": "计划-9", "signed_on": signed}).status_code == 201
    raw = {"pack_hash": pack["pack_hash"], "responses": [_answers("值班组长", text="由张经理负责审批交接")]}
    r = c.post(url, json=raw)
    assert r.status_code == 422 and "人员姓名" in r.text
    ok = {"pack_hash": pack["pack_hash"], "responses": [_answers("值班组长", text="交接时【系统A】告警未闭环")]}
    assert c.post(url, json=ok).status_code == 201


def test_nonmember_404_and_delete_clears():
    from app.project import deletion
    code = _project("standard")
    c = _client(code)
    pack = c.post(f"/api/projects/{code}/survey/packs").json()
    c.post(f"/api/projects/{code}/survey/packs/{pack['code']}/results",
           json={"pack_hash": pack["pack_hash"], "responses": [_answers("值班组长")]})
    outsider = logged_in_client(app, create_user(("consultant",)))
    assert outsider.get(f"/api/projects/{code}/survey/facts").status_code == 404
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        pid = p.id
        deletion.purge(db, p)
        db.commit()
        assert not db.scalars(select(SurveyAnswer).where(SurveyAnswer.project_id == pid)).all()
