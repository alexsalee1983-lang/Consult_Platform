"""成熟度与符合性视图（二期 A10）：驾驶舱按能力域显示成熟度初判（现状 → 目标）、符合性条和风险矩阵。

不建表，读取时现算：差距分析表、成熟度初判表取最近一次交付链“差距分析”步骤的产出；
D4 已签署时分数以 D4 里的“成熟度初判”表为准（顾问在 D4 里直接改，不另发确认卡，负责人 2026-11-16）；
产品配置 maturity_scores: false（数据安全）只给符合性条和风险矩阵，不给分数；只给项目成员。"""
import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.main import app
from app.models import AgentRun, AgentRunStep, Deliverable, Project, ProjectConfig
from app.products import config
from app.project import assessment

from .conftest import create_user, logged_in_client
from .test_deliverables import _base, _new_project

GAP = """## 差距分析表
| 控制项编号 | 能力域 | 要求要点 | 现状 | 证据编号 | 符合性初判 | 业务影响 | 影响 | 可能性 | 依据 | 整改方向 | 需补证据 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| G-01 | 组织与人员 | 7×24 值守 | 只有白班 | I-01 | 不符合 | 夜间告警无人看 | 高 | 高 | L4-1 | 夜间值守 | 无 |
| G-02 | 组织与人员 | 岗位职责 | 有职责但未考核 | D-01 | 部分符合 | 职责落空 | 中 | 中 | L4-1 | 加考核 | 无 |
| G-03 | 流程 | 事件分级 | 已分级 | D-02 | 符合 | — | 低 | 低 | L4-1 | — | 无 |
| G-04 | 技术平台与数据源 | 日志接入 | 未见材料 | — | 证据不足 | 不明 | 中 | 高 | L4-1 | 补材料 | 接入清单 |

统计：符合 1，部分符合 1，不符合 1，证据不足 1。

## 成熟度初判
| 能力域 | 现状（1～5） | 目标（1～5） | 依据 |
|---|---|---|---|
| 组织与人员 | 1.8 | 3 | I-01 |
| 流程 | 2 分 | 3.5 | D-02 |
| 技术平台与数据源 | 9 | 3 | 推断 |
"""


def _run(code: str, output: str = GAP) -> None:
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        run = AgentRun(project_id=p.id, status="done", created_by="t")
        db.add(run)
        db.flush()
        db.add(AgentRunStep(run_id=run.id, step="gap", agent_code="compliance-basis", status="done", output=output))
        db.commit()


@pytest.fixture()
def proj():
    code = _new_project()
    user = create_user(("consultant",), {code: "member"})
    c = logged_in_client(app, user)
    yield code, c
    c.__exit__(None, None, None)


def test_parse_tables():
    got = assessment.parse(GAP)
    assert [f["code"] for f in got["findings"]] == ["G-01", "G-02", "G-03", "G-04"]
    g1 = got["findings"][0]
    assert g1["domain"] == "组织与人员" and g1["conclusion"] == "unmet" and g1["impact"] == "高" and g1["likelihood"] == "高"
    assert got["findings"][3]["conclusion"] == "insufficient"
    assert got["scores"] == [{"domain": "组织与人员", "current": 1.8, "target": 3.0, "basis": "I-01"},
                             {"domain": "流程", "current": 2.0, "target": 3.5, "basis": "D-02"}]   # 9 分超出 1～5 不收
    assert assessment.parse("没有表格的正文")["findings"] == []


def test_empty_before_any_run(proj):
    code, c = proj
    r = c.get(f"/api/projects/{code}/assessment")
    assert r.status_code == 200
    d = r.json()
    assert d["source"] == "" and d["domains"] == [] and d["scores"] == [] and d["scores_enabled"] is True
    assert d["scores_label"] == "成熟度初判"


def test_from_gap_run(proj):
    code, c = proj
    _run(code)
    d = c.get(f"/api/projects/{code}/assessment").json()
    assert d["source"] == "gap" and d["scores_source"] == "gap"
    org = next(x for x in d["domains"] if x["domain"] == "组织与人员")
    assert org["counts"] == {"met": 0, "partial": 1, "unmet": 1, "insufficient": 0, "na": 0}
    assert [s["domain"] for s in d["scores"]] == ["组织与人员", "流程"]
    risks = {f["code"]: (f["impact"], f["likelihood"]) for f in d["findings"]}
    assert risks == {"G-01": ("高", "高"), "G-02": ("中", "中"), "G-04": ("中", "高")}   # 符合、不适用不进风险矩阵


def test_signed_d4_scores_win(proj):
    code, c = proj
    _run(code)
    table = "| 能力域 | 现状（1～5） | 目标（1～5） | 依据 |\n|---|---|---|---|\n| 组织与人员 | 2.1 | 3.2 | I-01 |\n"
    c.put(f"{_base(code)}/outline", json={"titles": ["现状", "成熟度初判"]})
    c.put(f"{_base(code)}/sections/1", json={"body": "现状概述。"})
    c.put(f"{_base(code)}/sections/2", json={"body": table})
    assert c.get(f"/api/projects/{code}/assessment").json()["scores_source"] == "gap"     # 未签署仍取差距分析
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        db.scalar(select(Deliverable).where(Deliverable.project_id == p.id, Deliverable.code == "D4")).status = "signed"
        db.commit()
    d = c.get(f"/api/projects/{code}/assessment").json()
    assert d["scores_source"] == "D4" and d["scores"] == [
        {"domain": "组织与人员", "current": 2.1, "target": 3.2, "basis": "I-01"}]
    assert d["source"] == "gap" and len(d["domains"]) == 3                             # 符合性仍取差距分析


def test_product_without_scores(proj):
    code, c = proj
    _run(code)
    snap = config.compose_codes(config.DEFAULT_PRODUCT)
    snap["maturity_scores"] = False
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        row = db.scalar(select(ProjectConfig).where(ProjectConfig.project_id == p.id))
        if row is None:
            db.add(ProjectConfig(project_id=p.id, product=snap["product"], modules=[], product_version="t",
                                 module_versions={}, snapshot=snap, created_by="t"))
        else:
            row.snapshot = snap
        db.commit()
    d = c.get(f"/api/projects/{code}/assessment").json()
    assert d["scores_enabled"] is False and d["scores"] == [] and len(d["findings"]) == 3


def test_catalog_flag():
    assert config.compose_codes(config.DEFAULT_PRODUCT)["maturity_scores"] is True
    dsm = config.catalog().products["DSM"]
    assert dsm.maturity_scores is False


def test_outsider_404(proj):
    code, _ = proj
    other = _new_project()
    with logged_in_client(app, create_user(("consultant",), {other: "member"})) as c2:
        assert c2.get(f"/api/projects/{code}/assessment").status_code == 404
