"""三期 B19 第二步：控制项映射 v0 的格式自检（knowledge/controls/mappings_v0.yaml）。

只校验结构和条款号是否存在于对应框架版本，不校验映射对错（内容待产品专家复核）。
关系类型与强度取值参照 NIST IR 8477、SCF STRM，与 C12 用同一套取值（三期计划第八节第 1 条）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "knowledge" / "controls" / "v0.yaml"
MAPPINGS = ROOT / "knowledge" / "controls" / "mappings_v0.yaml"

RELATIONS = {"等价", "子集", "超集", "交集"}
# 资质边界：映射理由不得出现结论性用语
FORBIDDEN = ("测评结论", "密评结论", "认证结论", "通过测评", "符合认证要求", "可通过审核", "满足等保")

# ISO/IEC 27001:2022 附录 A：5.1～5.37、6.1～6.8、7.1～7.14、8.1～8.34
ISO_ANNEX_A = {f"A.{t}.{n}" for t, top in ((5, 37), (6, 8), (7, 14), (8, 34)) for n in range(1, top + 1)}
# 正文条款（管理体系要求）
ISO_BODY = {"4.1", "4.2", "4.3", "4.4", "5.1", "5.2", "5.3", "6.1", "6.2", "6.3",
            "7.1", "7.2", "7.3", "7.4", "7.5", "8.1", "8.2", "8.3", "9.1", "9.2", "9.3", "10.1", "10.2"}

# NIST CSF 2.0：类别 → 最大子类编号；WITHDRAWN 为 2.0 中已撤销的编号
CSF_MAX = {
    "GV.OC": 5, "GV.RM": 7, "GV.RR": 4, "GV.PO": 2, "GV.OV": 3, "GV.SC": 10,
    "ID.AM": 8, "ID.RA": 10, "ID.IM": 4,
    "PR.AA": 6, "PR.AT": 2, "PR.DS": 11, "PR.PS": 6, "PR.IR": 4,
    "DE.CM": 9, "DE.AE": 8,
    "RS.MA": 5, "RS.AN": 8, "RS.CO": 3, "RS.MI": 2,
    "RC.RP": 6, "RC.CO": 4,
}
CSF_WITHDRAWN = {
    "ID.AM-06", "PR.DS-03", "PR.DS-04", "PR.DS-05", "PR.DS-06", "PR.DS-07", "PR.DS-08", "PR.DS-09",
    "DE.CM-04", "DE.CM-05", "DE.CM-07", "DE.CM-08", "DE.AE-01", "DE.AE-05",
    "RS.AN-01", "RS.AN-02", "RS.AN-04", "RS.AN-05", "RS.CO-01", "RC.CO-01", "RC.CO-02",
}
CSF_RE = re.compile(r"^(GV|ID|PR|DE|RS|RC)\.[A-Z]{2}-\d{2}$")


def _valid_clause(fw: str, clause: str) -> bool:
    if fw == "ISO27001":
        return clause in ISO_ANNEX_A or clause in ISO_BODY
    if fw == "CSF":
        if not CSF_RE.match(clause) or clause in CSF_WITHDRAWN:
            return False
        cat, num = clause.split("-")
        return cat in CSF_MAX and 1 <= int(num) <= CSF_MAX[cat]
    return False


@pytest.fixture(scope="module")
def data() -> tuple[dict, dict]:
    catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    mappings = yaml.safe_load(MAPPINGS.read_text(encoding="utf-8"))
    return catalog, mappings


def test_header(data: tuple[dict, dict]) -> None:
    catalog, m = data
    assert m["catalog_version"] == catalog["version"]
    assert m["status"] == "draft"
    assert m["expert_review"] == "pending"
    assert set(m["frameworks"]) == {"ISO27001", "CSF"}
    for fw in m["frameworks"].values():
        assert fw["name"] and fw["version"]


def test_every_control_mapped_to_each_framework(data: tuple[dict, dict]) -> None:
    catalog, m = data
    ids = {c["id"] for c in catalog["controls"]}
    assert set(m["mappings"]) == ids, "映射表与清单编号不一致"
    for cid, rows in m["mappings"].items():
        for fw in m["frameworks"]:
            assert any(r["fw"] == fw for r in rows), f"{cid} 缺 {fw} 映射"


def test_rows_well_formed(data: tuple[dict, dict]) -> None:
    _, m = data
    for cid, rows in m["mappings"].items():
        seen: set[tuple[str, str]] = set()
        for r in rows:
            assert set(r) == {"fw", "clause", "rel", "strength", "why"}, (cid, r)
            assert r["fw"] in m["frameworks"], (cid, r)
            assert _valid_clause(r["fw"], str(r["clause"])), (cid, r["clause"])
            assert r["rel"] in RELATIONS, (cid, r)
            assert isinstance(r["strength"], int) and 1 <= r["strength"] <= 10, (cid, r)
            assert r["rel"] != "等价" or r["strength"] >= 8, f"{cid}：等价映射强度应 ≥8"
            assert 2 <= len(r["why"]) <= 40, (cid, r["why"])
            assert not any(w in r["why"] for w in FORBIDDEN), (cid, r["why"])
            key = (r["fw"], str(r["clause"]))
            assert key not in seen, f"{cid} 重复映射 {key}"
            seen.add(key)


def test_clause_validator_rejects_bad_ids() -> None:
    assert _valid_clause("ISO27001", "A.5.24")
    assert not _valid_clause("ISO27001", "A.5.38")
    assert not _valid_clause("ISO27001", "A.12.4.1")  # 2013 版编号
    assert _valid_clause("CSF", "DE.AE-02")
    assert not _valid_clause("CSF", "DE.CM-04")  # 2.0 已撤销
    assert not _valid_clause("CSF", "RS.MA-06")
    assert not _valid_clause("CSF", "DE.DP-01")  # 1.1 类别
