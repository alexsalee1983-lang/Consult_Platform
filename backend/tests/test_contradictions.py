"""四期 A18：与确认卡、事实确认单矛盾的程序核对（阻断、可驳回、驳回后不重提），阻断误报率度量与自动校准
（误报偏高的类别降为提醒，机密与客户信息、资质用语不降），审计和问题栏不记选项原文。"""
import json

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import AuditLog, Confirmation, Project, SurveyAnswer, SurveyPack, SurveyQuestion
from app.project import contradictions

from .test_quality import BODY1, _draft, _review, _url, _waive, model, team  # noqa: F401

OPT_A = "夜间引入托管服务做告警初筛"
OPT_B = "夜间自建值守班组轮岗处置"
BODY_A = f"夜间无人值守 [O-01]；建议{OPT_A} [推断]。"
BODY_B = f"夜间无人值守 [O-01]；建议{OPT_B} [推断]。"


def _pid(code: str) -> int:
    with SessionLocal() as db:
        return db.scalar(select(Project.id).where(Project.code == code))


def _card(code: str, choice: str = "B", section: int | None = 2, deliverable: str = "D4") -> int:
    with SessionLocal() as db:
        c = Confirmation(project_id=_pid(code), kind="方案取舍", title="夜间值守方案", agent="方案设计 Agent",
                         status="resolved", options=[{"key": "A", "text": OPT_A}, {"key": "B", "text": OPT_B},
                                                     {"key": "C", "text": "暂缓"}],
                         choice=choice, deliverable_code=deliverable, section_seq=section)
        db.add(c)
        db.commit()
        return c.id


def _fact(code: str, value: str, qid: str) -> None:
    with SessionLocal() as db:
        if db.scalar(select(SurveyQuestion.id).where(SurveyQuestion.qid == qid)) is None:
            db.add(SurveyQuestion(qid=qid, control="SOC-ORG", kind="closed", text="夜间值守现状",
                                  options=[{"key": "A", "label": "夜间有专人值守并有交接记录"},
                                           {"key": "B", "label": "夜间无人值守只靠告警短信"}]))
        p = SurveyPack(project_id=_pid(code), code="P-01", pack_hash="0" * 64, tier="standard", qids=[qid],
                       created_by="t")
        db.add(p)
        db.flush()
        db.add(SurveyAnswer(project_id=p.project_id, pack_id=p.id, qid=qid, control="SOC-ORG",
                            respondent_role="安全运营负责人", kind="closed", value=value, fact_status="confirmed"))
        db.commit()


def _rule_hits(rv: dict) -> list[dict]:
    return [i for i in rv["issues"] if i.get("rule") == contradictions.RULE]


def test_contradicts_unit():
    body = contradictions._norm(BODY_A)
    assert contradictions._contradicts(body, OPT_B, [("A", OPT_A), ("C", "暂缓")]) == ["A"]   # “暂缓”太短不查
    assert contradictions._contradicts(body, OPT_A, [("B", OPT_B)]) == []
    both = contradictions._norm(BODY_A + OPT_B)
    assert contradictions._contradicts(both, OPT_B, [("A", OPT_A)]) == []                    # 写了所选项不算
    assert contradictions._contradicts(contradictions._norm("引入托管服务全天值守"), "引入托管服务全天值守",
                                       [("A", "引入托管服务")]) == []                          # 未选项是所选项一部分


def test_card_contradiction_blocks_waivable_then_not_reraised(team, model):  # noqa: F811
    code, member, mgr, _ = team
    cid = _card(code)
    _card(code, section=1)                       # 关联第 1 章的卡：第 1 章没写，不查第 2 章
    _card(code, deliverable="D5", section=None)  # 别的交付件
    _draft(member, code, bodies=(BODY1, BODY_A))
    rv = _review(member, code)
    hits = _rule_hits(rv)
    assert len(hits) == 1, hits
    h = hits[0]
    assert h["severity"] == "阻断" and h["waivable"] and h["category"] == "与已确认事项矛盾"
    assert f"#{cid}" in h["problem"] and OPT_A not in h["problem"] and h["location"].startswith("第 2 章")
    assert rv["blocking"] == 1 and rv["open_blocking"] == 1
    st = member.get(_url(code, "signoff")).json()
    assert not st["gate"]

    idx = rv["issues"].index(h)
    r = _waive(mgr, code, rv["id"], idx, reason="项目经理复核：正文是对比两个方案，不是采纳 A")
    assert r.status_code == 200, r.text
    assert member.get(_url(code, "signoff")).json()["gate"]

    assert member.put(_url(code, "sections/1"), json={"body": BODY1 + "补充。"}).status_code == 201
    rv2 = _review(member, code)
    h2 = _rule_hits(rv2)
    assert len(h2) == 1 and h2[0]["status"] == "已驳回" and h2[0]["code"] == h["code"]
    assert rv2["blocking"] == 0 and member.get(_url(code, "signoff")).json()["gate"]

    with SessionLocal() as db:
        dump = json.dumps([x.detail for x in db.scalars(select(AuditLog).where(AuditLog.project_code == code))],
                          ensure_ascii=False)
    assert OPT_A not in dump and OPT_B not in dump


def test_chosen_option_written_no_issue(team, model):  # noqa: F811
    code, member, *_ = team
    _card(code)
    _draft(member, code, bodies=(BODY1, BODY_B))
    assert _rule_hits(_review(member, code)) == []


def test_survey_fact_contradiction(team, model):  # noqa: F811
    code, member, *_ = team
    _fact(code, "B", f"T-{code}")
    _draft(member, code, bodies=("SOC 现状：夜间有专人值守并有交接记录 [O-01]。", BODY_B))
    hits = _rule_hits(_review(member, code))
    assert len(hits) == 1 and f"T-{code}" in hits[0]["problem"] and hits[0]["location"].startswith("第 1 章")
    assert "交接记录" not in hits[0]["problem"]


def test_calibration_downgrades_high_waive_rate_but_not_protected(team, model, monkeypatch):  # noqa: F811
    code, member, mgr, _ = team
    _card(code)
    _draft(member, code, bodies=(BODY1, BODY_A))
    rv = _review(member, code)
    idx = rv["issues"].index(_rule_hits(rv)[0])
    assert _waive(mgr, code, rv["id"], idx, reason="项目经理复核：正文是对比两个方案，不是采纳 A").status_code == 200
    with SessionLocal() as db:
        stats = {(x["by"], x["category"]): x for x in contradictions.false_positive_stats(db)}
    s = stats[("rule", "与已确认事项矛盾")]
    assert s["total"] >= 1 and s["waived"] >= 1 and 0 < s["rate"] <= 1

    monkeypatch.setattr(contradictions, "CALIBRATE_MIN", 1)
    monkeypatch.setattr(contradictions, "CALIBRATE_RATE", 0.0)
    assert member.put(_url(code, "sections/2"), json={"body": BODY_A + "再补一句。"}).status_code == 201
    rv2 = _review(member, code)
    h = _rule_hits(rv2)
    assert h and h[0]["severity"] == "重要" and h[0]["calibrated"] and not h[0]["waivable"]
    assert rv2["blocking"] == 0
    with SessionLocal() as db:
        a = db.scalars(select(AuditLog).where(AuditLog.project_code == code, AuditLog.action == "quality.review")
                       .order_by(AuditLog.id.desc())).first()
        assert json.loads(a.detail)["calibrated"] >= 1

    keys = {("agent", "资质用语"), ("agent", "依据"), ("rule", "机密与客户信息")}
    issues = [{"by": "agent", "severity": "阻断", "category": "资质用语"},
              {"by": "agent", "severity": "阻断", "category": "依据"},
              {"by": "rule", "severity": "阻断", "category": "机密与客户信息", "rule": contradictions.RULE}]
    counts = {"阻断": 2, "重要": 0, "建议": 0}
    assert contradictions.apply_calibration(issues, counts, keys) == 1
    assert [i["severity"] for i in issues] == ["阻断", "重要", "阻断"] and counts == {"阻断": 1, "重要": 1, "建议": 0}
    with SessionLocal() as db:
        assert all(not x["calibrated"] for x in contradictions.false_positive_stats(db)
                   if x["category"] in contradictions.PROTECTED)
