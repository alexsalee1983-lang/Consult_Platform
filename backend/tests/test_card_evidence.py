"""四期 A32②：确认卡证据并排与同源合卡（只改展示，不改卡的状态机）。
卡片文字里引用的证据（[I-01-02] [O-01] [D-03]）取原文片段并排给出，标页码（PDF 按页解析的“第 N 页”）和段落号；
同一份资料引出的多张待处理卡合成一组；非项目成员 404。"""
import secrets

import pytest
from sqlalchemy import select

from app.agents import tools
from app.core.db import SessionLocal
from app.main import app
from app.models import Confirmation, Deliverable, Project, User
from app.project import card_evidence
from app.seed import DELIVERABLES

from .conftest import create_user, logged_in_client


@pytest.fixture()
def proj():
    code = f"CEV-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行")
        p.deliverables = [Deliverable(code=f"D{i + 1}", title=t) for i, t in enumerate(DELIVERABLES)]
        db.add(p)
        db.commit()
    user = create_user(("consultant",), {code: "member"})
    c = logged_in_client(app, user)
    assert c.post(f"/api/projects/{code}/notes", json={
        "kind": "interview", "title": "值班访谈", "interviewee_role": "SOC 负责人",
        "content": "目前全部白班。\n夜间告警由值班手机接收，次日处理。\n交接班记录为纸质。"}).status_code == 201
    assert c.post(f"/api/projects/{code}/notes", json={
        "kind": "observation", "title": "值班室巡查",
        "content": "值班台有夜班排班表。\n夜间有两人在岗。"}).status_code == 201
    yield code, c, user[0]
    c.__exit__(None, None, None)


def _send(name: str, code: str, title: str, reason: str) -> int:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == name))
        r = tools.invoke(db, user, "solution-designer", "confirm.create",
                         {"kind": "来源冲突", "title": title, "options": ["A 以访谈为准", "B 以现场观察为准"],
                          "recommendation": "B", "reason": reason, "impact": "影响夜班人力测算"}, code)
        return r["id"]


def test_evidence_side_by_side_with_paragraphs_and_same_source_merged(proj):
    code, c, name = proj
    a = _send(name, code, "夜间是否有人值守？", "访谈 [I-01-02] 说夜间无人，现场 [O-01] 看到夜间有两人在岗")
    b = _send(name, code, "交接班记录形式？", "访谈 [I-01-03] 说纸质记录，需确认电子化计划")
    other = _send(name, code, "排班表是否覆盖节假日？", "现场 [O-01-01] 只看到工作日排班")
    lone = _send(name, code, "是否纳入外包？", "无直接证据，按前提推断")

    r = c.get(f"/api/projects/{code}/confirmations/evidence")
    assert r.status_code == 200, r.text
    out = r.json()
    pair = out["evidence"][str(a)]
    assert [x["code"] for x in pair] == ["I-01", "O-01"]                 # 两处证据并排
    assert pair[0]["paragraph"] == 2 and pair[0]["excerpt"] == "夜间告警由值班手机接收，次日处理。"
    assert pair[0]["title"] == "值班访谈" and pair[0]["page"] is None
    assert pair[1]["paragraph"] == 2 and "两人在岗" in pair[1]["excerpt"]  # 没写段落号：按卡片文字找最相近的段落
    assert out["evidence"][str(lone)] == []

    groups = {tuple(g["card_ids"]): g for g in out["groups"]}
    assert groups[(a, b)]["source"] == "I-01" and groups[(a, b)]["title"] == "值班访谈"   # 同一份资料引出的合成一组
    assert (other,) in groups and (lone,) in groups

    with SessionLocal() as db:                                            # 只读：卡的状态不变
        assert {x.status for x in db.scalars(select(Confirmation).where(Confirmation.id.in_([a, b])))} == {"open"}

    assert c.post(f"/api/confirmations/{a}/resolve", json={"choice": "B"}).status_code == 200
    groups = [g["card_ids"] for g in c.get(f"/api/projects/{code}/confirmations/evidence").json()["groups"]]
    assert [b] in groups and not any(a in g for g in groups)              # 默认只看待处理的卡


def test_locate_marks_pdf_page_and_paragraph():
    sections = [{"heading": "第 1 页", "text": "封面"}, {"heading": "第 2 页", "text": "值班制度\n夜间实行电话值守"}]
    hit = card_evidence.locate(sections, 3, "")
    assert hit == {"page": 2, "paragraph": 3, "heading": "第 2 页", "excerpt": "夜间实行电话值守"}
    hit = card_evidence.locate(sections, None, "夜间是否电话值守")
    assert hit["page"] == 2 and hit["paragraph"] == 3
    assert card_evidence.locate([], None, "x") is None


def test_non_member_404(proj):
    code, _, _ = proj
    outsider = logged_in_client(app, create_user(("consultant",)))
    try:
        assert outsider.get(f"/api/projects/{code}/confirmations/evidence").status_code == 404
    finally:
        outsider.__exit__(None, None, None)
