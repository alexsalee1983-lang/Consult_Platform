"""三期 F1：发现表（字段对齐 OSCAL 评估结果与 POA&M、责任方）、状态推进、训练三元组（默认关）、项目删除。"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import Finding, JudgmentEvent, Project
from app.project import findings, judgment

from .test_assessment import GAP
from .test_judgment import _new_project
from .test_runs import _note, _work, model, proj  # noqa: F401  （fixture）

GAP_RERUN = GAP.replace("| G-02 | 组织与人员 | 岗位职责 | 有职责但未考核 | D-01 | 部分符合 |",
                        "| G-02 | 组织与人员 | 岗位职责 | 有职责但未考核 | D-01、D-03 | 不符合 |") \
               .replace("| G-03 | 流程 | 事件分级 | 已分级 | D-02 | 符合 | — | 低 | 低 | L4-1 | — | 无 |\n", "") \
               + "\n| 控制项编号 | 能力域 | 要求要点 | 符合性初判 |\n|---|---|---|---|\n| G-05 | 流程 | 复盘 | 不符合 |\n"


@pytest.fixture()
def project():
    code = _new_project()
    with SessionLocal() as db:
        yield db, db.scalar(select(Project).where(Project.code == code))


def _rows(db, p) -> dict[str, Finding]:
    return {f.control_code: f for f in findings.list_for_project(db, p)}


def test_parse_gap_fields() -> None:
    got = findings.parse_gap(GAP)
    assert [f["control_code"] for f in got] == ["G-01", "G-02", "G-03", "G-04"]
    g1, g3, g4 = got[0], got[2], got[3]
    assert g1["conclusion"] == g1["agent_conclusion"] == "unmet"
    assert g1["evidence_refs"] == ["I-01"] and g1["basis_clause"] == "L4-1"
    assert g1["impact"] == "高" and g1["likelihood"] == "高" and g1["risk"] == "夜间告警无人看"
    assert g1["recommendation"] == "夜间值守" and g1["current"] == "只有白班"
    assert g1["current_score"] is not None and g1["target_score"] is not None   # 按能力域取成熟度初判
    assert g3["risk"] == "" and g3["recommendation"] == ""                       # “—”当空
    assert g4["evidence_refs"] == []


def test_sync_assigns_stable_f_codes(project) -> None:
    db, p = project
    assert findings.sync_from_gap(db, p, GAP)["added"] == 4
    rows = _rows(db, p)
    assert [rows[c].fid for c in ("G-01", "G-02", "G-03", "G-04")] == ["F-01", "F-02", "F-03", "F-04"]
    assert all(f.status == "draft" and f.source == "agent" for f in rows.values())

    findings.advance(db, p, rows["G-01"], "confirmed")                # 顾问确认过的，重跑不动
    rows["G-01"].conclusion = "partial"
    counts = findings.sync_from_gap(db, p, GAP_RERUN.replace("| 不符合 | 夜间告警无人看", "| 符合 | 夜间告警无人看"))
    assert counts == {"added": 1, "updated": 2, "kept": 1, "removed": 1}
    rows = _rows(db, p)
    assert "G-03" not in rows                                           # Agent 初判这次没了：删
    assert rows["G-01"].conclusion == "partial" and rows["G-01"].status == "confirmed"
    assert rows["G-02"].conclusion == "unmet" and rows["G-02"].evidence_refs == ["D-01", "D-03"]
    assert rows["G-05"].fid == "F-05"                                    # 接着最大编号，不复用 F-03
    db.rollback()


def test_advance_one_step_only(project) -> None:
    db, p = project
    findings.sync_from_gap(db, p, GAP)
    f = _rows(db, p)["G-01"]
    with pytest.raises(findings.FindingError) as e:
        findings.advance(db, p, f, "signed")
    assert e.value.status == 409
    for to in findings.STATUSES[1:]:
        findings.advance(db, p, f, to)
    assert f.status == "verified"
    with pytest.raises(findings.FindingError):
        findings.advance(db, p, f, "draft")
    db.rollback()


def test_update_validates_and_locks_after_sign(project) -> None:
    db, p = project
    findings.sync_from_gap(db, p, GAP)
    f = _rows(db, p)["G-01"]
    findings.update(db, f, {"responsibility": "inherited", "priority": "高", "owner_dept": "信息科技部",
                            "period": "3 个月", "acceptance": "夜间值守排班表上线", "current_score": 2,
                            "evidence_refs": ["I-01", "I-02"]})
    assert f.responsibility == "inherited" and f.current_score == 2.0
    for bad in ({"responsibility": "vendor"}, {"conclusion": "pass"}, {"current_score": 6},
                {"current_score": True}, {"fid": "F-99"}, {"status": "signed"}, {"evidence_refs": "I-01"},
                {"owner_dept": "x" * 65}):
        with pytest.raises(findings.FindingError) as e:
            findings.update(db, f, bad)
        assert e.value.status == 422, bad
    with pytest.raises(findings.FindingError) as e:
        findings.update(db, f, {"recommendation": "管理口令 password=Abc12345!x 改掉"})
    assert e.value.status == 422 and "机密级" in e.value.detail
    findings.advance(db, p, f, "confirmed")
    findings.advance(db, p, f, "signed")
    with pytest.raises(findings.FindingError) as e:
        findings.update(db, f, {"priority": "低"})
    assert e.value.status == 409
    db.rollback()


def _events(db, p) -> list[JudgmentEvent]:
    return list(db.scalars(select(JudgmentEvent).where(JudgmentEvent.project_id == p.id,
                                                       JudgmentEvent.kind == "finding")))


def test_triple_off_by_default(project) -> None:
    db, p = project
    assert get_settings().judgment_capture is False
    findings.sync_from_gap(db, p, GAP)
    findings.advance(db, p, _rows(db, p)["G-01"], "confirmed")
    assert _events(db, p) == []
    db.rollback()


def test_triple_on_confirm_without_text(project, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "judgment_capture", True)
    db, p = project
    findings.sync_from_gap(db, p, GAP)
    f = _rows(db, p)["G-01"]
    findings.update(db, f, {"conclusion": "partial", "responsibility": "shared"})
    findings.advance(db, p, f, "confirmed")
    findings.advance(db, p, f, "signed")                                 # 只在确认时记一次
    (e,) = _events(db, p)
    assert e.category == "partial" and e.agent_code == "compliance-basis"
    assert e.attrs["agent"] == "unmet" and e.attrs["human"] == "partial" and e.attrs["agreed"] is False
    assert e.attrs["control"] == "G-01" and e.attrs["evidence"] == 1 and e.attrs["responsibility"] == "shared"
    dumped = repr(e.attrs)
    for text in ("夜间", "白班", "值守", "7×24"):                          # 不记现状、风险、建议、要求原文
        assert text not in dumped
    db.flush()
    got = [t for t in judgment.triples(db) if t["features"]["control"] == "G-01" and t["human"] == "partial"]
    assert got and got[-1]["agent"] == "unmet" and set(got[-1]["features"]) >= {"industry", "evidence"}
    db.rollback()


def test_consultant_findings_untouched_by_sync(project) -> None:
    db, p = project
    db.add(Finding(project_id=p.id, fid="F-01", control_code="G-09", item="顾问补充", conclusion="unmet",
                   source="consultant", evidence_refs=[]))
    db.flush()
    counts = findings.sync_from_gap(db, p, GAP)
    rows = _rows(db, p)
    assert counts["removed"] == 0 and rows["G-09"].fid == "F-01" and rows["G-01"].fid == "F-02"
    db.rollback()


def test_project_deletion_removes_findings(project) -> None:
    db, p = project
    findings.sync_from_gap(db, p, GAP)
    db.commit()
    assert findings.delete_for_project(db, p.id) == 4
    db.commit()
    assert findings.list_for_project(db, p) == []


def test_chain_gap_step_syncs_findings(proj, model) -> None:  # noqa: F811
    """交付链第 4 步（依据与差距分析）跑完，差距分析表同步成发现，记运行编号。"""
    code, c = proj
    model.replies["交付链第 4 步"] = GAP
    _note(c, code)
    rid = c.post(f"/api/projects/{code}/runs").json()["id"]
    _work()
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        rows = findings.list_for_project(db, p)
        assert [f.fid for f in rows] == ["F-01", "F-02", "F-03", "F-04"]
        assert all(f.run_id == rid and f.agent_code == "compliance-basis" for f in rows)
