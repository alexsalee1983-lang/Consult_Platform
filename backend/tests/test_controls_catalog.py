"""三期 B19 第一步：控制项清单 v0 的格式自检（knowledge/controls/v0.yaml）。

清单供 B15 的 labels.check_control 和 B19 的 kb_controls 种子数据使用；这里只校验结构，
不校验内容对错（内容待产品专家复核）。
"""

from __future__ import annotations

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
