"""判断数据埋点（4A 架构设计 v4.0 §7.2，决策记录 2026-11-18）：

- 平台运行数据，直接启用：项目组织类型（立项可选、取值固定）、交付链运行记录的配置哈希、差距分析的 F 编号；
- 判断数据（顾问改 Agent 稿的幅度、确认卡的选择）只在 JUDGMENT_CAPTURE 打开时记，默认关（等法务意见），
  只记计数和类别，不记正文、标题、选项文字和意见；项目删除时一并删除。"""
from __future__ import annotations

import secrets

import pytest
from sqlalchemy import select

from app.agents import tools
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.permissions import CONSULTANT, PROJECT_LEAD
from app.main import app
from app.models import AgentRun, Deliverable, JudgmentEvent, Project, User
from app.project import assessment, configs, judgment, runs
from app.seed import DELIVERABLES

from .conftest import create_user, logged_in_client
from .test_assessment import GAP
from .test_confirmations import CARD

AGENT_BODY = "SOC 现有 6 人，全部白班，夜间告警由值班经理次日查看。[推断]"


def _new_project(org_type: str = "") -> str:
    code = f"JDG-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier="standard", org_type=org_type)
        p.deliverables = [Deliverable(code=f"D{i + 1}", title=t) for i, t in enumerate(DELIVERABLES)]
        db.add(p)
        db.commit()
    return code


@pytest.fixture()
def proj():
    code = _new_project("central_soe")
    account = create_user((CONSULTANT,), {code: "manager"})  # 四期 A29：发起交付链只给项目经理
    c = logged_in_client(app, account)
    yield code, c, account[0]
    c.__exit__(None, None, None)


@pytest.fixture()
def capture(monkeypatch):
    monkeypatch.setattr(get_settings(), "judgment_capture", True)


def _events(code: str) -> list[JudgmentEvent]:
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        return list(db.scalars(select(JudgmentEvent).where(JudgmentEvent.project_id == p.id).order_by(JudgmentEvent.id)))


def _agent_write(code: str, name: str, body: str = AGENT_BODY) -> None:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == name))
        tools.invoke(db, user, "report-writer", "doc.generate",
                     {"action": "outline", "deliverable": "D4", "sections": ["现状", "差距"]}, code)
        tools.invoke(db, user, "report-writer", "doc.generate",
                     {"action": "write", "deliverable": "D4", "section": 1, "body": body}, code)


# ---- 组织类型（平台运行数据） ----

def test_org_type_on_create() -> None:
    lead = logged_in_client(app, create_user((PROJECT_LEAD,)))
    try:
        code = f"QA-{secrets.token_hex(3).upper()}"
        base = {"tier": "standard", "product": "SOC", "industry": "电力"}
        bad = lead.post("/api/projects", json={**base, "code": code, "org_type": "神秘机构"})
        assert bad.status_code == 422
        r = lead.post("/api/projects", json={**base, "code": code, "org_type": "local_soe"})
        assert r.status_code == 201, r.text
        assert r.json()["org_type"] == "local_soe" and r.json()["org_type_name"] == "地方国企"
        other = f"QA-{secrets.token_hex(3).upper()}"
        assert lead.post("/api/projects", json={**base, "code": other}).json()["org_type"] == ""   # 可不填
    finally:
        lead.__exit__(None, None, None)
    assert set(configs.ORG_TYPES) >= {"central_soe", "local_soe", "private", "foreign", "government", "institution"}


# ---- 配置哈希（平台运行数据） ----

def test_run_records_config_sha(proj, monkeypatch) -> None:
    code, c, name = proj
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        sha = runs.config_sha(db, p)
        assert len(sha) == 64 and runs.config_sha(db, p) == sha                     # 同样配置同样哈希
        monkeypatch.setattr(get_settings(), "llm_model_standard", "other-model")
        assert runs.config_sha(db, p) != sha                                        # 换模型哈希变
        monkeypatch.undo()
    c.post(f"/api/projects/{code}/notes",
           json={"kind": "observation", "title": "值班室巡查", "content": "值班台交接记录为纸质，夜间无人值守。"})
    r = c.post(f"/api/projects/{code}/runs")
    assert r.status_code == 202, r.text
    with SessionLocal() as db:
        run = db.get(AgentRun, r.json()["id"])
        assert run.config_sha == runs.config_sha(db, db.get(Project, run.project_id))
    assert r.json()["config_sha"] == run.config_sha


# ---- F 编号（平台运行数据） ----

def test_findings_get_platform_f_codes() -> None:
    got = assessment.parse(GAP)["findings"]
    assert [f["fid"] for f in got] == ["F-01", "F-02", "F-03", "F-04"]
    assert [f["code"] for f in got] == ["G-01", "G-02", "G-03", "G-04"]          # Agent 写的编号照旧保留


# ---- 判断数据：默认关 ----

def test_capture_off_by_default(proj) -> None:
    code, c, name = proj
    assert get_settings().judgment_capture is False
    _agent_write(code, name)
    assert c.put(f"/api/projects/{code}/deliverables/D4/sections/1", json={"body": "顾问重写：夜间托管。"}).status_code == 201
    assert _events(code) == []


def test_edit_magnitude_after_agent_draft(proj, capture) -> None:
    code, c, name = proj
    _agent_write(code, name)
    body = AGENT_BODY.replace("值班经理次日查看", "托管方初筛后电话通知")
    assert c.put(f"/api/projects/{code}/deliverables/D4/sections/1", json={"body": body}).status_code == 201
    assert c.put(f"/api/projects/{code}/deliverables/D4/sections/1",
                 json={"body": body + "补一句。"}).status_code == 201                  # 顾问改顾问：不记
    evs = _events(code)
    assert len(evs) == 1
    e = evs[0]
    assert e.kind == "edit" and e.deliverable_code == "D4" and e.section_seq == 1 and e.agent_code == "report-writer"
    assert 0 < e.magnitude < 1 and e.category in judgment.EDIT_BUCKETS
    assert e.attrs["from_version"] == 1 and e.attrs["to_version"] == 2
    dump = repr((e.category, e.attrs))
    assert "托管" not in dump and "值班" not in dump                                    # 不记正文


def test_card_choice_recorded_without_text(proj, capture) -> None:
    code, c, name = proj
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == name))
        card = tools.invoke(db, user, "solution-designer", "confirm.create", CARD, code)
        other = tools.invoke(db, user, "solution-designer", "confirm.create",
                             {**CARD, "title": "夜间值守方式？"}, code)
    assert c.post(f"/api/confirmations/{card['id']}/resolve", json={"choice": "A"}).status_code == 200
    assert c.post(f"/api/confirmations/{other['id']}/resolve",
                  json={"choice": "B", "resolution": "预算另批"}).status_code == 200
    a, b = _events(code)
    assert a.kind == b.kind == "card" and a.category == "方案取舍" and a.agent_code == "solution-designer"
    assert a.deliverable_code == "D5"
    assert a.attrs == {"options": 2, "chose": True, "followed": True, "commented": False}
    assert b.attrs == {"options": 2, "chose": True, "followed": False, "commented": True}
    dump = repr([(e.category, e.attrs) for e in (a, b)])
    assert "人力" not in dump and "扩编" not in dump and "预算" not in dump


def test_project_deletion_removes_events(proj, capture) -> None:
    code, c, name = proj
    _agent_write(code, name)
    c.put(f"/api/projects/{code}/deliverables/D4/sections/1", json={"body": "顾问重写：夜间托管。"})
    assert _events(code)
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        judgment.delete_for_project(db, p.id)
        db.commit()
    assert _events(code) == []
