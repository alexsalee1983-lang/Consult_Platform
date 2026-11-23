"""三期 B19：控制项中枢表结构（知识区 k0013）与 app/kb/controls.py。

导入 #237 控制项清单 / #245 映射格式（这里用虚构的 TST 编号）、重复导入幂等、关系类型与强度校验、
资质用语拦截、适用性规则、检查点、框架换版列出受影响映射。
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.core.db import SessionLocal
from app.kb import controls
from app.models import KbControl, KbControlCheckpoint, KbControlMapping, KbFramework

CATALOG = {
    "version": "t0",
    "controls": [
        {"id": "TST.ORG.01", "name": "职责与编制", "product": "SOC", "domain": "组织与人员", "industries": ["通用"],
         "checkpoints": [{"id": "a", "objective": "已书面明确值班、分析、处置岗位职责", "methods": ["examine", "interview"]}],
         "params": [{"id": "log_retention_months", "label": "日志保留月数", "default": 6}]},
        {"id": "TST.BNK.01", "name": "监管报送衔接", "product": "IRE", "domain": "通报与报告", "industries": ["银行"],
         "applicability": {"industries": ["银行"], "jurisdictions": ["CN"], "mlps_levels": [3, 4]}},
    ],
}
MAPPINGS = {
    "catalog_version": "t0",
    "frameworks": {"TSTFW": {"name": "测试框架", "version": "2022"}},
    "mappings": {
        "TST.ORG.01": [{"fw": "TSTFW", "clause": "A.5.2", "rel": "交集", "strength": 7, "why": "角色与职责分配"}],
        "TST.BNK.01": [{"fw": "TSTFW", "clause": "A.5.24", "rel": "子集", "strength": 5, "why": "事件报告"}],
    },
}


@pytest.fixture()
def db():
    s = SessionLocal()
    yield s
    s.rollback()
    ids = select(KbControl.id).where(KbControl.code.like("TST.%"))
    s.execute(delete(KbControlMapping).where(KbControlMapping.control_id.in_(ids)))
    s.execute(delete(KbControlCheckpoint).where(KbControlCheckpoint.control_id.in_(ids)))
    s.execute(delete(KbControl).where(KbControl.code.like("TST.%")))
    s.execute(delete(KbFramework).where(KbFramework.code == "TSTFW", KbFramework.superseded_by_id.is_not(None)))
    s.execute(delete(KbFramework).where(KbFramework.code == "TSTFW"))
    s.commit()
    s.close()


def test_import_is_idempotent_and_keeps_structure(db):
    first = controls.import_catalog(db, CATALOG, MAPPINGS)
    db.commit()
    assert first == {"controls": 2, "checkpoints": 1, "mappings": 2, "frameworks": 1}
    again = controls.import_catalog(db, CATALOG, MAPPINGS)
    db.commit()
    assert again == {"controls": 0, "checkpoints": 0, "mappings": 0, "frameworks": 0}
    c = db.scalar(select(KbControl).where(KbControl.code == "TST.ORG.01"))
    assert c.catalog_version == "t0" and c.params[0]["id"] == "log_retention_months"
    cp = db.scalar(select(KbControlCheckpoint).where(KbControlCheckpoint.control_id == c.id))
    assert cp.code == "TST.ORG.01.a" and cp.methods == ["examine", "interview"]
    m = db.scalar(select(KbControlMapping).where(KbControlMapping.control_id == c.id))
    assert (m.relation, m.strength, m.mapping_version) == ("交集", 7, "t0")


@pytest.mark.parametrize("row, msg", [
    ({"fw": "TSTFW", "clause": "A.1", "rel": "相关", "strength": 5, "why": "x"}, "关系类型"),
    ({"fw": "TSTFW", "clause": "A.1", "rel": "等价", "strength": 11, "why": "x"}, "强度"),
    ({"fw": "NOPE", "clause": "A.1", "rel": "等价", "strength": 5, "why": "x"}, "框架"),
    ({"fw": "TSTFW", "clause": "A.1", "rel": "等价", "strength": 5, "why": "可通过审核"}, "资质"),
])
def test_bad_mapping_rejected(db, row, msg):
    bad = {**MAPPINGS, "mappings": {"TST.ORG.01": [row]}}
    with pytest.raises(controls.ControlDataError, match=msg):
        controls.import_catalog(db, CATALOG, bad)


def test_bad_checkpoint_method_and_unknown_control_rejected(db):
    cat = {"version": "t0", "controls": [{**CATALOG["controls"][0],
                                          "checkpoints": [{"id": "a", "objective": "x", "methods": ["guess"]}]}]}
    with pytest.raises(controls.ControlDataError, match="检查方法"):
        controls.import_catalog(db, cat)
    with pytest.raises(controls.ControlDataError, match="不在清单"):
        controls.import_catalog(db, CATALOG, {**MAPPINGS, "mappings": {"TST.XXX.99": []}})


def test_relation_values_shared_with_c12():
    assert controls.RELATIONS == ("等价", "子集", "超集", "交集")


def test_applicability_rules():
    rule = {"industries": ["银行"], "jurisdictions": ["CN"], "mlps_levels": [3, 4]}
    assert controls.applies(rule, {"industry": "银行", "jurisdiction": "CN", "mlps_level": 3})
    assert not controls.applies(rule, {"industry": "电力", "jurisdiction": "CN", "mlps_level": 3})
    assert not controls.applies(rule, {"industry": "银行", "jurisdiction": "CN", "mlps_level": 2})
    assert controls.applies(rule, {"industry": "银行"})            # 未给出的维度不排除
    assert controls.applies({}, {"industry": "电力"})               # 空规则 = 不限


def test_framework_supersede_lists_affected_mappings(db):
    controls.import_catalog(db, CATALOG, MAPPINGS)
    db.commit()
    impact = controls.supersede_framework(db, "TSTFW", "2022", new_version="2025", new_name="测试框架 2025")
    db.commit()
    assert impact["framework"] == "TSTFW 2022" and impact["superseded_by"] == "TSTFW 2025"
    assert {(r["control"], r["clause"]) for r in impact["mappings"]} == {("TST.ORG.01", "A.5.2"), ("TST.BNK.01", "A.5.24")}
    assert impact["controls"] == ["TST.BNK.01", "TST.ORG.01"]
    old = db.scalar(select(KbFramework).where(KbFramework.code == "TSTFW", KbFramework.version == "2022"))
    assert old.status == "superseded" and old.superseded_by_id is not None
    with pytest.raises(controls.ControlDataError):
        controls.supersede_framework(db, "TSTFW", "2022", new_version="2026", new_name="x")
