"""立项快照的合并（二期 A4，docs/接口约定.md 第三、四节）：产品 + 模块 → 自足的完整配置。"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.products import config
from app.project import runs

from .test_product_config import AGENTS, MODULE, PRODUCT, _write


def _cat(tmp_path: Path, product: dict = PRODUCT, module: dict = MODULE) -> config.Catalog:
    cat = config.load(_write(tmp_path, product, module), agents=AGENTS)
    assert cat.problems == []
    return cat


def test_product_only(tmp_path: Path) -> None:
    cat = _cat(tmp_path)
    snap = config.compose(cat.products["SOC"])
    assert snap["product"] == "SOC" and snap["modules"] == []
    assert snap["product_version"] == cat.products["SOC"].version
    facts, gap, draft = snap["chain"]
    assert facts["instruction"] == "按 SOC 运营体系设计 能力域（组织与人员、流程）分章。"
    assert facts["query"] == "SOC 运营体系设计 现状"
    assert gap["instruction"] == "对照 SOC 能力框架、GB/T 22239-2019。"
    assert draft["instruction"] == "第 7 步。"
    assert snap["gates"][0]["items"] == ["A.5.1", "A.5.2"] and snap["gates"][0]["source"] == "SOC"
    assert [d["code"] for d in snap["deliverables"]] == ["D1", "D2", "D3"]
    json.dumps(snap, ensure_ascii=False)              # 能存进 JSON 列


def test_module_merges(tmp_path: Path) -> None:
    cat = _cat(tmp_path)
    snap = config.compose(cat.products["SOC"], [cat.modules["DJBH"]])
    assert snap["modules"] == [{"code": "DJBH", "name": "等保 2.0 整改与体系建设", "version": cat.modules["DJBH"].version}]
    assert snap["frameworks"] == ["SOC 能力框架", "等保 2.0 第三级"]
    gap = snap["chain"][1]
    assert gap["instruction"] == "对照 SOC 能力框架、等保 2.0 第三级、GB/T 22239-2019。逐项对照第三级管理要求。"
    d3 = next(d for d in snap["deliverables"] if d["code"] == "D3")
    assert d3["outline"] == ["等保整改专项"]
    assert [g["id"] for g in snap["gates"]] == ["annex", "no-cert", "djbh"]
    assert snap["gates"][2]["source"] == "DJBH" and snap["kb_tags"] == ["等保"]


def test_module_instruction_appended_without_variable(tmp_path: Path) -> None:
    p = copy.deepcopy(PRODUCT)
    p["chain"][1]["instruction"] = "对照基线。"
    cat = _cat(tmp_path, p)
    gap = config.compose(cat.products["SOC"], [cat.modules["DJBH"]])["chain"][1]
    assert gap["instruction"] == "对照基线。\n逐项对照第三级管理要求。"


def test_snapshot_is_self_contained(tmp_path: Path) -> None:
    cat = _cat(tmp_path)
    snap = config.compose(cat.products["SOC"], [cat.modules["DJBH"]])
    (tmp_path / "products" / "rules" / "annex.txt").write_text("A.9.9\n", encoding="utf-8")
    assert snap["gates"][0]["items"] == ["A.5.1", "A.5.2"]
    chain = runs.chain_of(snap)
    assert [(s.key, s.deliverable) for s in chain] == [("facts", "D2"), ("gap", ""), ("draft", "D3")]
    assert chain[1].instruction.endswith("逐项对照第三级管理要求。")


def test_compose_rejects(tmp_path: Path) -> None:
    cat = _cat(tmp_path)
    soc, djbh = cat.products["SOC"], cat.modules["DJBH"]
    with pytest.raises(config.CatalogError, match="重复"):
        config.compose(soc, [djbh, djbh])
    other = copy.deepcopy(PRODUCT)
    other.update(code="ISO", name="ISO", modules=[])
    iso = config.load(_write(tmp_path / "b", other, None), agents=AGENTS).products["ISO"]
    with pytest.raises(config.CatalogError, match="不能挂"):
        config.compose(iso, [djbh])
    from dataclasses import replace
    with pytest.raises(config.CatalogError, match="ready"):
        config.compose(soc, [replace(djbh, status="draft")])
    with pytest.raises(config.CatalogError, match="ready"):
        config.compose(replace(soc, status="draft"))


def test_check_snapshot() -> None:
    for bad in (None, [], {"format": 0, "chain": []}, {"format": 1}):
        with pytest.raises(config.CatalogError):
            config.check_snapshot(bad)
