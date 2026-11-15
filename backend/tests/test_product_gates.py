"""门禁规则按产品（二期 A6）：通用层（平台代码）+ 产品层 + 模块层（项目配置快照里的 gates）。

产品、模块规则判出的问题 by = rule，阻断不能驳回；只查本交付件（forbidden_terms 不写交付件 = 全部）；
快照读不出来时记一条阻断、不放行；问题描述只有规则编号、配置清单项和计数，不摘正文。
"""
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import Project, ProjectConfig
from app.products import config
from app.project import configs

from .test_quality import BODY1, BODY2, _draft, _review, _waive, model, team  # noqa: F401  （夹具）

GATES = [
    {"id": "d4-sections", "kind": "required_sections", "deliverable": "D4", "level": "major", "source": "SOC",
     "message": "缺必备章节", "sections": ["现状概述", "整改建议"]},
    {"id": "annex", "kind": "coverage", "deliverable": "D4", "level": "blocking", "source": "DJBH",
     "message": "未覆盖全部控制项", "items": ["值守", "A.5.1"]},
    {"id": "no-term", "kind": "forbidden_terms", "deliverable": "", "level": "blocking", "source": "SOC",
     "message": "不用这个说法", "terms": ["托管", "不相干"]},
    {"id": "sources", "kind": "min_sources", "deliverable": "D4", "level": "major", "source": "SOC",
     "message": "来源不足", "min": 2},
    {"id": "other", "kind": "required_sections", "deliverable": "D5", "level": "blocking", "source": "SOC",
     "message": "别的交付件", "sections": ["不相关"]},
]


def _set_gates(code: str, gates: list[dict]) -> None:
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        snap = config.compose_codes("SOC")
        snap["gates"] = gates
        row = db.scalar(select(ProjectConfig).where(ProjectConfig.project_id == p.id))
        if row is None:
            configs._store(db, p, snap, "test")
        else:
            row.snapshot = snap
        db.commit()


def test_product_and_module_gates_apply(team, model):  # noqa: F811  （夹具来自 test_quality）
    code, member, mgr, _ = team
    _set_gates(code, GATES)
    _draft(member, code)
    rv = _review(member, code)
    mine = [i for i in rv["issues"] if i["dimension"].startswith("产品规则")]
    got = sorted((i["dimension"], i["severity"], i["location"]) for i in mine)
    assert got == sorted([
        ("产品规则 SOC/d4-sections", "重要", "大纲"),
        ("产品规则 DJBH/annex", "阻断", "全文"),
        ("产品规则 SOC/no-term", "阻断", "第 2 章 主要差距"),
        ("产品规则 SOC/sources", "重要", "第 1 章 现状概述"),
        ("产品规则 SOC/sources", "重要", "第 2 章 主要差距"),
    ])
    annex = next(i for i in mine if i["dimension"].endswith("annex"))
    assert "A.5.1" in annex["problem"] and "值守" not in annex["problem"]          # 只列缺的项
    assert "整改建议" in next(i for i in mine if i["location"] == "大纲")["problem"]
    assert all(i["by"] == "rule" and not i["waivable"] for i in mine)
    assert all(BODY1 not in i["problem"] and BODY2 not in i["problem"] for i in mine)   # 不摘正文
    assert rv["blocking"] == 2 and rv["conclusion"] == "fail"
    idx = next(n for n, i in enumerate(rv["issues"]) if i["dimension"].endswith("annex"))
    assert _waive(mgr, code, rv["id"], idx).status_code == 422                     # 平台规则的阻断不能驳回


def test_no_product_gates_means_only_common_rules(team, model):  # noqa: F811  （夹具来自 test_quality）
    code, member, _, _ = team
    _set_gates(code, [])
    _draft(member, code)
    rv = _review(member, code)
    assert not [i for i in rv["issues"] if i["dimension"].startswith("产品规则")]
    assert rv["blocking"] == 0


def test_broken_snapshot_blocks(team, model):  # noqa: F811  （夹具来自 test_quality）
    code, member, _, _ = team
    _draft(member, code)
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        row = db.scalar(select(ProjectConfig).where(ProjectConfig.project_id == p.id))
        if row is None:
            row = configs._store(db, p, config.compose_codes("SOC"), "test")
        row.snapshot = {"format": 0}
        db.commit()
    rv = _review(member, code)
    broken = [i for i in rv["issues"] if i["dimension"] == "产品规则"]
    assert len(broken) == 1 and broken[0]["severity"] == "阻断" and not broken[0]["waivable"]


def test_coverage_matches_whole_item_only():
    from app.project.quality import covers
    text = "SoA：5.10 已覆盖；A.5.12 不适用；15.1 无；8.34。值守安排见第 2 章。"
    assert not covers(text, "5.1") and not covers(text, "A.5.1") and not covers(text, "8.3")
    assert covers(text, "5.10") and covers(text, "A.5.12") and covers(text, "8.34") and covers(text, "值守")
    assert covers("控制 5.1、5.2 已实施", "5.1") and covers("（5.1）", "5.1") and covers("5.1\n", "5.1")


def test_iso_soa_gate_lists_all_93_annex_a_controls():
    """D1：ISO 产品的 SoA 门禁清单是附录 A 2022 版 93 项控制号（5.1～5.37、6.1～6.8、7.1～7.14、8.1～8.34）。"""
    from app.project.quality import covers
    cat = config.load()
    assert not cat.problems
    iso = cat.products["ISO"]
    assert iso.status == "draft"
    soa = next(g for g in iso.gates if g.id == "soa-annex-a")
    items = list(soa.params["items"])
    want = [f"{a}.{i}" for a, n in ((5, 37), (6, 8), (7, 14), (8, 34)) for i in range(1, n + 1)]
    assert items == want and len(items) == 93
    full = "\n".join(f"{c} 适用" for c in want)
    assert all(covers(full, c) for c in items)
    assert [c for c in items if not covers(full.replace("5.1 适用\n", ""), c)] == ["5.1"]
def test_dsm_draft_config_loads_with_boundary_gate():
    """D2：数据安全产品草稿可加载；资质边界门禁覆盖全部交付件。"""
    cat = config.load()
    assert not cat.problems
    dsm = cat.products["DSM"]
    assert dsm.status == "draft" and [d.code for d in dsm.deliverables] == [f"D{i}" for i in range(1, 9)]
    g = next(g for g in dsm.gates if g.id == "no-assessment-claim")
    assert g.deliverable == "" and g.level == "blocking"
