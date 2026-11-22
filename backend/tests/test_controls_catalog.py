"""三期 B19 第一步：控制项清单 v0 的格式自检（knowledge/controls/v0.yaml）。

清单供 B15 的 labels.check_control 和 B19 的 kb_controls 种子数据使用；这里只校验结构，
不校验内容对错（内容待产品专家复核）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.kb.items import CONTROL_ID_RE

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "knowledge" / "controls" / "v0.yaml"
QUESTION_BANK = ROOT / "question_bank"

PRODUCTS = {"SOC", "IRE"}
INDUSTRIES = {"通用", "银行", "电力"}
STATUSES = {"draft", "active", "retired"}
# 资质边界：控制项名称不得出现结论性用语
FORBIDDEN = ("测评结论", "密评结论", "认证结论", "通过测评", "符合认证要求", "可通过审核")


@pytest.fixture(scope="module")
def catalog() -> dict:
    return yaml.safe_load(CATALOG.read_text(encoding="utf-8"))


def test_header(catalog: dict) -> None:
    assert catalog["version"] == "v0"
    assert catalog["status"] == "draft"
    assert catalog["expert_review"] == "pending"
    assert set(catalog["domains"]) == PRODUCTS


def test_count_in_range(catalog: dict) -> None:
    assert 60 <= len(catalog["controls"]) <= 90


def test_fields_and_values(catalog: dict) -> None:
    seen: set[str] = set()
    for c in catalog["controls"]:
        cid = c["id"]
        assert CONTROL_ID_RE.match(cid), cid
        assert cid == cid.upper(), cid
        assert cid not in seen, f"重复编号 {cid}"
        seen.add(cid)
        assert c["product"] in PRODUCTS, cid
        assert cid.startswith(c["product"] + "."), cid
        assert c["domain"] in catalog["domains"][c["product"]], cid
        inds = c["industries"]
        assert inds and set(inds) <= INDUSTRIES, cid
        assert not ("通用" in inds and len(inds) > 1), f"{cid}：通用不和具体行业同时出现"
        assert c.get("status", "draft") in STATUSES, cid
        assert 2 <= len(c["name"]) <= 32, cid
        assert not any(w in c["name"] for w in FORBIDDEN), cid


def test_four_grid_coverage(catalog: dict) -> None:
    """四格（SOC、IRE × 银行、电力）每格都有行业专属控制项，每个评估域至少 2 项。"""
    for p in PRODUCTS:
        for ind in ("银行", "电力"):
            assert any(c["product"] == p and ind in c["industries"] for c in catalog["controls"]), (p, ind)
        for d in catalog["domains"][p]:
            n = sum(1 for c in catalog["controls"] if c["product"] == p and c["domain"] == d)
            assert n >= 2, (p, d)


def test_question_bank_controls_included(catalog: dict) -> None:
    """题库（E1）里的 controls 必须在清单里，名称和评估域一致；题目引用的编号必须在清单里。"""
    banks = sorted(QUESTION_BANK.glob("*.yaml")) if QUESTION_BANK.is_dir() else []
    if not banks:
        pytest.skip("题库尚未合并（#223）")
    by_id = {c["id"]: c for c in catalog["controls"]}
    for path in banks:
        bank = yaml.safe_load(path.read_text(encoding="utf-8"))
        for c in bank.get("controls", []):
            assert c["id"] in by_id, f"{path.name}: {c['id']}"
            assert by_id[c["id"]]["name"] == c["name"], c["id"]
            assert by_id[c["id"]]["domain"] == c["domain"], c["id"]
        for q in bank.get("questions", []):
            for cid in q.get("control_ids", []):
                assert cid in by_id, f"{path.name}: {cid}"


# ---- 检查点（三期 B19，挂知识区 kb_control_checkpoints，参照 SP 800-53A 评估目标）----
METHODS = {"examine", "interview", "test"}
CHECKPOINT_ID_RE = re.compile(r"^[a-z]$")
PARAM_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
APPLICABILITY_KEYS = {"industries", "mlps_levels", "jurisdictions", "deployments"}
# 检查点写可核查的事实，不写结论性用语（导入时会拒收）
OBJECTIVE_FORBIDDEN = FORBIDDEN + ("测评", "认证", "合格", "达标", "合规性结论", "满足等保")


def test_checkpoints_structure(catalog: dict) -> None:
    for c in catalog["controls"]:
        cid = c["id"]
        cps = c.get("checkpoints")
        assert isinstance(cps, list) and 2 <= len(cps) <= 4, f"{cid}：检查点须 2～4 个"
        ids = [cp["id"] for cp in cps]
        assert len(set(ids)) == len(ids), f"{cid}：检查点编号重复"
        assert ids == [chr(ord("a") + i) for i in range(len(ids))], f"{cid}：检查点编号须 a、b、c… 连续"
        for cp in cps:
            assert set(cp) <= {"id", "objective", "methods"}, (cid, cp["id"])
            assert CHECKPOINT_ID_RE.match(cp["id"]), (cid, cp["id"])
            obj = cp["objective"]
            assert isinstance(obj, str) and 6 <= len(obj) <= 60, (cid, cp["id"])
            assert not any(w in obj for w in OBJECTIVE_FORBIDDEN), (cid, cp["id"], obj)
            m = cp["methods"]
            assert m and set(m) <= METHODS and len(set(m)) == len(m), (cid, cp["id"])


def test_applicability_and_params(catalog: dict) -> None:
    for c in catalog["controls"]:
        cid = c["id"]
        app = c.get("applicability")
        if c["industries"] != ["通用"]:
            assert app and app.get("industries") == c["industries"], f"{cid}：行业专属项须补 applicability"
        if app is not None:
            assert set(app) <= APPLICABILITY_KEYS, cid
            for k, v in app.items():
                assert isinstance(v, list) and v, (cid, k)
            if "industries" in app:
                assert set(app["industries"]) <= INDUSTRIES - {"通用"}, cid
            if "mlps_levels" in app:
                assert set(app["mlps_levels"]) <= {1, 2, 3, 4, 5}, cid
        seen: set[str] = set()
        for p in c.get("params", []):
            assert set(p) == {"id", "label", "default"}, (cid, p)
            assert PARAM_ID_RE.match(p["id"]) and p["id"] not in seen, (cid, p["id"])
            seen.add(p["id"])
            assert isinstance(p["label"], str) and p["label"], (cid, p["id"])
            assert isinstance(p["default"], (int, float, str)), (cid, p["id"])


def test_params_present_for_obvious_cases(catalog: dict) -> None:
    """保留期、响应时限、演练频次等组织定义参数至少覆盖到这些项。"""
    by_id = {c["id"]: c for c in catalog["controls"]}
    for cid in ("SOC.TEC.05", "SOC.PRC.02", "SOC.PRC.05", "IRE.GOV.02", "IRE.RPT.02"):
        assert by_id[cid].get("params"), cid
