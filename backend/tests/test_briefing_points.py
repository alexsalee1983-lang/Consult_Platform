"""四期 A39：D8 从真源取要点——发现表（D4 真源）与 D5 / D7 当前版本由程序摘出，交给 D8 起草步骤。"""
from __future__ import annotations

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.permissions import CONSULTANT
from app.models import Project, User
from app.project import briefing, deliverables, findings

from .conftest import create_user
from .test_assessment import GAP
from .test_findings import project  # noqa: F401  （fixture）
from .test_runs import _note, _work, _run, model, proj  # noqa: F401  （fixture）


def _rows(db, p):
    return {f.control_code: f for f in findings.list_for_project(db, p)}


def test_points_from_findings_only_confirmed(project) -> None:  # noqa: F811
    db, p = project
    assert briefing.points(db, p) == ""                                 # 什么都没有：不加块
    findings.sync_from_gap(db, p, GAP)
    assert briefing.points(db, p) == ""                                 # 只有草稿发现：不算真源
    rows = _rows(db, p)
    for code in ("G-01", "G-02", "G-03"):
        findings.advance(db, p, rows[code], "confirmed")
    rows["G-02"].priority = "高"
    text = briefing.points(db, p)
    assert text.startswith(briefing.MARK)
    assert "已确认发现 3 条" in text and "不符合 1" in text and "部分符合 1" in text and "符合 1" in text
    lines = [x for x in text.splitlines() if x.startswith("- [F-")]
    assert lines[0].startswith("- [F-02]")                               # 高优先级排前
    assert any(x.startswith("- [F-01]") and "夜间告警无人看" in x for x in lines)
    assert not any(x.startswith("- [F-03]") for x in lines)             # 符合项不进要点
    assert not any(x.startswith("- [F-04]") for x in lines)             # 草稿不进
    db.rollback()


def test_points_from_d5_d7_sections(project) -> None:  # noqa: F811
    db, p = project
    username = create_user((CONSULTANT,), {p.code: "member"})[0]
    u = db.scalar(select(User).where(User.username == username))
    d5 = deliverables.get_deliverable(db, p, "D5")
    deliverables.set_outline(db, u, p, d5, ["目标运营模型", "人员测算"])
    deliverables.write_section(db, u, p, d5, 1, "| 表头 |\n|---|\n\n- 夜间初筛加白班分析 [O-01]。后续再说。\n第二行")
    text = briefing.points(db, p)
    assert "- [D5 §目标运营模型] 夜间初筛加白班分析 [O-01]。" in text
    assert "后续再说" not in text and "人员测算" not in text           # 每章只取首句；没有正文的章略过
    assert "[D7 §" not in text                                          # D7 没有正文
    db.rollback()


def test_d8_step_gets_points_block(proj, model) -> None:  # noqa: F811
    code, c = proj
    _note(c, code)
    rid = c.post(f"/api/projects/{code}/runs").json()["id"]
    _work()
    assert _run(c, code, rid)["status"] == "done"
    prompt = model.prompt("起草 D8")
    assert briefing.MARK in prompt and "[D5 §设计目标与原则]" in prompt and "[D7 §目标运营模型]" in prompt
    assert briefing.MARK not in model.prompt("起草 D5")                 # 只给 D8
    with SessionLocal() as db:
        assert db.scalar(select(Project).where(Project.code == code)) is not None
