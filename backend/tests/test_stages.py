"""四期 A27：阶段自动推进（程序规则：本阶段交付件全部签署即进入下一阶段，只前进不后退，推进记审计 actor=platform）、
启动/分析/验收三段的阶段包（汇报 PPT 草稿 + 各交付件 Word）、本周简报（自动完成 / 人工处理 / 待办，只记计数不记原文）。"""
import io
import json
import zipfile
from datetime import timedelta

import pytest
from pptx import Presentation
from sqlalchemy import select

from app.core import audit
from app.core.config import get_settings
from app.core.db import SessionLocal, utcnow
from app.main import app
from app.models import AuditLog, Confirmation, Project
from app.project import stages

from .conftest import create_user, logged_in_client
from .test_deliverables import _base, _new_project


@pytest.fixture()
def auto_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "auto_flow", True)


def _project(db, code: str) -> Project:
    return db.scalar(select(Project).where(Project.code == code))


def _sign(code: str, *dcodes: str) -> None:
    with SessionLocal() as db:
        p = _project(db, code)
        for d in p.deliverables:
            if d.code in dcodes:
                d.status = "signed"
        db.commit()


def _phase(code: str) -> int:
    with SessionLocal() as db:
        return _project(db, code).phase


def _advances(code: str) -> list[dict]:
    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                                 AuditLog.action == "project.phase_auto").order_by(AuditLog.id))
        return [{"actor": a.actor, **json.loads(a.detail)} for a in rows]


def test_auto_advance_rules(auto_on):
    code = _new_project()
    with SessionLocal() as db:
        assert stages.scan(db, _project(db, code)) == []                 # D1 没签：停在启动
    assert _phase(code) == 0
    _sign(code, "D1")
    with SessionLocal() as db:
        assert stages.scan(db, _project(db, code)) == [code]
        assert stages.scan(db, _project(db, code)) == []                 # 可重复执行，不重复推进
    assert _phase(code) == 1
    adv = _advances(code)
    assert adv == [{"actor": "platform", "from_phase": "启动", "to_phase": "调研", "trigger": "stage_exit"}]

    _sign(code, "D2")                                                     # D3 还没签：停在调研
    with SessionLocal() as db:
        assert stages.scan(db, _project(db, code)) == []
    _sign(code, "D3", "D4", "D5", "D6", "D7")                             # 汇报段没有交付件：直接到验收
    with SessionLocal() as db:
        assert stages.scan(db, _project(db, code)) == [code]
    assert _phase(code) == 5
    assert _advances(code)[-1]["to_phase"] == "验收"

    with SessionLocal() as db:                                            # 只前进不后退
        p = _project(db, code)
        p.deliverables[0].status = "drafting"
        db.commit()
        assert stages.scan(db, p) == []
    assert _phase(code) == 5


def test_auto_advance_off_or_closed(monkeypatch):
    code = _new_project()
    _sign(code, "D1")
    with SessionLocal() as db:
        assert stages.scan(db, _project(db, code)) == []                 # 开关关（测试默认）：什么也不做
    monkeypatch.setattr(get_settings(), "auto_flow", True)
    with SessionLocal() as db:
        p = _project(db, code)
        p.deleted_at = utcnow()
        db.commit()
        assert stages.scan(db, p) == []                                   # 已删除不推进
    assert _phase(code) == 0


@pytest.fixture()
def team():
    code = _new_project()
    mgr = create_user(("consultant",), {code: "manager"})
    c = logged_in_client(app, mgr)
    out = create_user(("consultant",), {"MFG-C": "member"})
    o = logged_in_client(app, out)
    yield code, c, o, mgr[0]
    c.__exit__(None, None, None)
    o.__exit__(None, None, None)


def test_stage_overview(team):
    code, c, o, _ = team
    _sign(code, "D1")
    r = c.get(f"/api/projects/{code}/stages")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current"] == "启动" and body["target"] == "调研"
    first = body["phases"][0]
    assert first["name"] == "启动" and first["done"] is True and first["report"] is True
    assert first["deliverables"] == [{"code": "D1", "title": first["deliverables"][0]["title"], "status": "signed"}]
    assert [p["report"] for p in body["phases"]] == [True, False, True, False, False, True]
    assert o.get(f"/api/projects/{code}/stages").status_code == 404


def test_stage_pack_zip_with_report_and_word(team):
    code, c, o, name = team
    c.put(f"{_base(code)}/outline", json={"titles": ["一句话结论"]})
    assert c.put(f"{_base(code)}/sections/1", json={"body": "**要点**\n- 夜间无人值守 [推断]"}).status_code == 201
    r = c.get(f"/api/projects/{code}/stages/分析/pack")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    ppt = next(n for n in names if n.endswith(".pptx"))
    assert "分析" in ppt and any("D4" in n and n.endswith(".docx") for n in names)
    prs = Presentation(io.BytesIO(z.read(ppt)))
    texts = ["\n".join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame) for s in prs.slides]
    assert code in texts[0] and "分析" in texts[0]
    assert all("草稿" in t for t in texts)                                # 汇报 PPT 一律草稿，人审
    assert any("夜间无人值守" in t for t in texts)
    assert "D4" in z.read("清单.txt").decode("utf-8")

    with SessionLocal() as db:
        a = db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                              AuditLog.action == "project.stage_pack")).all()
    assert len(a) == 1 and a[0].actor == name
    assert "夜间无人值守" not in a[0].detail and json.loads(a[0].detail)["phase"] == "分析"

    assert c.get(f"/api/projects/{code}/stages/调研/pack").status_code == 422   # 只有启动、分析、验收出汇报
    assert c.get(f"/api/projects/{code}/stages/不存在/pack").status_code == 422
    assert o.get(f"/api/projects/{code}/stages/分析/pack").status_code == 404


def test_weekly_brief_counts(team):
    code, c, o, name = team
    with SessionLocal() as db:
        p = _project(db, code)
        p.deliverables[0].status = "review"
        db.add(Confirmation(project_id=p.id, kind="contradiction", title="虚拟问题", agent="测试 Agent"))
        db.commit()
        audit.record(db, actor="platform", action="quality.request", result="allowed", project_code=code)
        audit.record(db, actor="platform", action="project.phase_auto", result="allowed", project_code=code)
        audit.record(db, actor=name, action="deliverable.sign", result="allowed", project_code=code)
        audit.record(db, actor=name, action="deliverable.export", result="allowed", project_code=code)  # 只读，不算
        audit.record(db, actor=name, action="deliverable.sign", result="denied", project_code=code)     # 被拒，不算
    r = c.get(f"/api/projects/{code}/weekly-brief")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["auto"]["total"] == 2 and b["manual"]["total"] == 1
    assert b["manual"]["items"] == [{"action": "deliverable.sign", "label": "签署交付件", "count": 1}]
    assert b["todo"]["confirmations"] == 1
    assert b["todo"]["awaiting_sign"] == ["D1"]
    assert "本周自动完成 2 项" in b["text"] and "虚拟问题" not in b["text"]
    with SessionLocal() as db:
        b2 = stages.weekly_brief(db, _project(db, code), now=utcnow() + timedelta(days=8))
    assert b2["auto"]["total"] == 0                                        # 下周看不到上周
    assert o.get(f"/api/projects/{code}/weekly-brief").status_code == 404
