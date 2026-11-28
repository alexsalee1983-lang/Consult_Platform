"""四期 A35：改稿——审阅清单、意图识别与扩散、人工定稿保护、不满意刷新、一键回退（docs/交付链输入输出方案.md 第七节）。"""
import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.main import app
from app.models import Project, QualityReview, User
from app.project import deliverables as dsvc
from app.project import rework
from app.services import jobs

from .conftest import create_user, logged_in_client
from .test_deliverables import _base, _new_project

AGENT_BODY = "现状：值班交接仍为纸质记录，交接要点缺少统一模板，夜间值班的事件升级路径没有书面规定 [O-01]。"
LONG_NO_SOURCE = "安全运营中心目前没有形成稳定的事件分级标准，导致夜间值班人员难以判断升级时机。"


@pytest.fixture()
def proj():
    code = _new_project()
    user = create_user(("consultant",), {code: "member"})
    c = logged_in_client(app, user)
    yield code, c, user[0]
    c.__exit__(None, None, None)


def _agent_write(code: str, name: str, seq: int, body: str, note: str = "交付链起草", dcode: str = "D4") -> None:
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        u = db.scalar(select(User).where(User.username == name))
        d = dsvc.require_deliverable(db, p, dcode)
        dsvc.write_section(db, u, p, d, seq, body, agent_code="report-writer", note=note)


def _outline(c, code: str, dcode: str = "D4", titles=("现状概述", "主要差距")) -> None:
    assert c.put(f"{_base(code, dcode)}/outline", json={"titles": list(titles)}).status_code == 200


def test_classify_fact_tone_wording():
    assert rework.classify("覆盖率 60%，见 [I-01]。", "覆盖率 80%，见 [I-01]。")["intent"] == rework.FACT
    assert rework.classify("见 [I-01]。", "见 [I-02]。")["intent"] == rework.FACT
    assert rework.classify("客户应建立值班制度。", "贵行应建立值班制度。")["intent"] == rework.TONE
    longer = "应建立值班制度，并按季度复核执行情况，形成闭环记录，逐项落实。"
    assert rework.classify("应建立值班制度。", longer)["intent"] == rework.TONE
    out = rework.classify("应建立值班制度，明确交接要求。", "应建立值班制度，明确交接规范。")
    assert out["intent"] == rework.WORDING and out["changes"]


def test_review_list_ranks_and_marks_human_final(proj):
    code, c, name = proj
    _outline(c, code, titles=("现状概述", "主要差距", "整改建议"))
    _agent_write(code, name, 1, AGENT_BODY + "\n\n" + LONG_NO_SOURCE + "\n\n可能与人员配置有关 [推断]。")
    assert c.put(f"{_base(code)}/sections/2", json={"body": "差距：缺少分级标准 [O-01]。"}).status_code == 201
    r = c.get(f"{_base(code)}/review-list")
    assert r.status_code == 200, r.text
    data = r.json()
    secs = {s["seq"]: s for s in data["sections"]}
    assert secs[1]["human_final"] is False and secs[2]["human_final"] is True
    kinds = [(i["seq"], i["kind"]) for i in data["items"]]
    assert (3, "缺正文") in kinds and (1, "无来源句子") in kinds and (1, "低置信") in kinds
    ranks = [i["rank"] for i in data["items"]]
    assert ranks == sorted(ranks)
    assert data["round"] == 1 and not any(s["changed"] for s in data["sections"])
    assert {x["reason"] for x in data["reasons"]} == set(rework.REASONS)


def test_second_round_highlights_changed_sections(proj):
    code, c, name = proj
    _outline(c, code)
    _agent_write(code, name, 1, AGENT_BODY)
    _agent_write(code, name, 2, "差距：缺少分级标准 [O-01]。")
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        d = dsvc.require_deliverable(db, p, "D4")
        db.add(QualityReview(project_id=p.id, deliverable_id=d.id, revision=d.revision, versions={"1": 1, "2": 1},
                             status="done", created_by=name, issues=[{"code": "Q-001", "severity": "阻断", "location": "第 2 章",
                                                     "problem": "对客结论无证据", "status": "open", "by": "agent"}]))
        db.commit()
    assert c.put(f"{_base(code)}/sections/1", json={"body": AGENT_BODY + "补充说明。"}).status_code == 201
    data = c.get(f"{_base(code)}/review-list").json()
    secs = {s["seq"]: s for s in data["sections"]}
    assert data["round"] == 2 and secs[1]["changed"] and not secs[2]["changed"]
    top = data["items"][0]
    assert top["kind"] == "门禁提醒" and top["seq"] == 2 and "Q-001" in top["hint"]


def test_fact_edit_marks_other_sections_citing_same_evidence(proj):
    code, c, name = proj
    _outline(c, code)
    _outline(c, code, "D5", ("整改路线",))
    _agent_write(code, name, 1, "现状：交接记录覆盖率 60% [O-01]。")
    _agent_write(code, name, 2, "差距：无关内容 [O-02]。")
    _agent_write(code, name, 1, "路线：先补交接模板 [O-01]。", dcode="D5")
    assert c.put(f"{_base(code)}/sections/1", json={"body": "现状：交接记录覆盖率 80% [O-01]。"}).status_code == 201
    r = c.post(f"{_base(code)}/sections/1/intent")
    assert r.status_code == 200, r.text
    assert r.json()["intent"] == "事实" and r.json()["marked"] == 1
    d5 = c.get(f"{_base(code, 'D5')}/review-list").json()
    assert d5["sections"][0]["stale"].startswith("事实更正") and d5["items"][0]["kind"] == "门禁提醒"
    d4 = {s["seq"]: s for s in c.get(f"{_base(code)}/review-list").json()["sections"]}
    assert d4[1]["stale"] == "" and d4[2]["stale"] == ""


def test_tone_edit_asks_project_wide(proj):
    code, c, name = proj
    _outline(c, code)
    _agent_write(code, name, 1, "客户应建立值班制度 [O-01]。")
    assert c.put(f"{_base(code)}/sections/1", json={"body": "贵行应建立值班制度 [O-01]。"}).status_code == 201
    out = c.post(f"{_base(code)}/sections/1/intent").json()
    assert out["intent"] == "口径" and out["ask_project_wide"] is True and out["marked"] == 0


def test_refresh_non_redraft_reasons_redirect(proj):
    code, c, _ = proj
    for reason, action in (("事实不对", "fact"), ("口径不对", "customization"), ("结构不对", "outputs")):
        r = c.post(f"{_base(code)}/refresh", json={"reason": reason})
        assert r.status_code == 202 and r.json()["action"] == action and r.json()["job_id"] is None
    assert c.post(f"{_base(code)}/refresh", json={"reason": "随便"}).status_code == 422


def test_refresh_skips_human_final_and_hands_off_after_two_rounds(proj, monkeypatch):
    code, c, name = proj
    monkeypatch.setattr(rework.runtime, "ready_doc", lambda db, agent: None)
    calls: list[int] = []

    def fake_rewrite(db, user, project, d, seq, items):
        calls.append(seq)
        return f"重写第 {len(calls)} 次：值班交接要点统一成模板 [O-01]。"

    monkeypatch.setattr(rework.revise, "_rewrite", fake_rewrite)
    _outline(c, code)
    _agent_write(code, name, 1, AGENT_BODY)
    _agent_write(code, name, 2, "差距：缺少分级标准 [O-01]。")
    assert c.put(f"{_base(code)}/sections/2", json={"body": "差距：缺少事件分级标准 [O-01]。"}).status_code == 201

    def refresh_once():
        r = c.post(f"{_base(code)}/refresh", json={"reason": "写得不好"})
        assert r.status_code == 202, r.text
        with SessionLocal() as db:
            while jobs.run_once(db, "test-worker", kinds=[rework.JOB_KIND]):
                pass
        return r.json()

    first = refresh_once()
    assert first["action"] == "redraft" and first["seqs"] == [1] and first["skipped"] == [2]
    refresh_once()
    third = refresh_once()
    assert third["action"] == "handoff" and third["handoff"] == [1] and third["job_id"] is None
    assert calls == [1, 1]
    sec = c.get(f"{_base(code)}/sections/1").json()
    assert sec["current_version"] == 3 and sec["note"].startswith("不满意刷新第 2 轮")
    sec2 = c.get(f"{_base(code)}/sections/2").json()
    assert sec2["current_version"] == 2                                       # 人工定稿未被覆盖
    # 一键回退到起草版：人选的版本算人工定稿，刷新轮数清零
    r = c.post(f"{_base(code)}/sections/1/rollback", json={"version": 1})
    assert r.status_code == 201 and r.json()["version"] == 4
    secs = {s["seq"]: s for s in c.get(f"{_base(code)}/review-list").json()["sections"]}
    assert secs[1]["human_final"] and secs[1]["refresh_rounds"] == 0
    assert c.post(f"{_base(code)}/sections/1/rollback", json={"version": 9}).status_code == 422
    nothing = c.post(f"{_base(code)}/refresh", json={"reason": "写得不好"}).json()
    assert nothing["action"] == "none" and nothing["skipped"] == [1, 2]
