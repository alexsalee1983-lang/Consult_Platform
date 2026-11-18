"""产品、模块配置的“待专家复核”标记（二期，决策记录 2026-11-18：无专家先行，公开方法版 v0 定稿、专家入场后复核）。"""
from __future__ import annotations

import copy
from pathlib import Path

from app.products import config
from app.project import configs
from tests.test_product_config import AGENTS, MODULE, PRODUCT, _problems, _write


def test_expert_review_defaults_to_pending(tmp_path: Path) -> None:
    cat = config.load(_write(tmp_path, PRODUCT, MODULE), agents=AGENTS)
    assert cat.problems == []
    assert cat.products["SOC"].expert_review == "pending"
    assert cat.modules["DJBH"].expert_review == "pending"


def test_expert_review_values(tmp_path: Path) -> None:
    prod = {**copy.deepcopy(PRODUCT), "expert_review": "done"}
    mod = {**copy.deepcopy(MODULE), "expert_review": "done"}
    cat = config.load(_write(tmp_path, prod, mod), agents=AGENTS)
    assert cat.problems == []
    assert cat.products["SOC"].expert_review == "done"
    bad = {**copy.deepcopy(PRODUCT), "expert_review": "ok"}
    assert any("pending 或 done" in p for p in _problems(tmp_path, bad, MODULE))
    badm = {**copy.deepcopy(MODULE), "expert_review": True}
    assert any("pending 或 done" in p for p in _problems(tmp_path, PRODUCT, badm))


def test_snapshot_lists_pending(tmp_path: Path) -> None:
    cat = config.load(_write(tmp_path, PRODUCT, MODULE), agents=AGENTS)
    snap = config.compose(cat.products["SOC"], [cat.modules["DJBH"]])
    assert snap["expert_pending"] == ["SOC", "DJBH"]
    prod = {**copy.deepcopy(PRODUCT), "expert_review": "done"}
    cat = config.load(_write(tmp_path, prod, MODULE), agents=AGENTS)
    assert config.compose(cat.products["SOC"], [cat.modules["DJBH"]])["expert_pending"] == ["DJBH"]


def test_options_and_project_info_carry_marker(tmp_path: Path, monkeypatch) -> None:
    cat = config.load(_write(tmp_path, PRODUCT, MODULE), agents=AGENTS)
    monkeypatch.setattr(config, "catalog", lambda: cat)
    soc = next(o for o in configs.options() if o["code"] == "SOC")
    assert soc["expert_review"] == "pending"
    assert soc["modules"] == [{"code": "DJBH", "name": MODULE["name"], "expert_review": "pending"}]

    snap = config.compose(cat.products["SOC"], [cat.modules["DJBH"]])
    monkeypatch.setattr(configs, "snapshot", lambda db, p: snap)
    assert configs.info(None, None)["expert_pending"] == ["SOC", "DJBH"]
    old = {k: v for k, v in snap.items() if k != "expert_pending"}     # 标记之前立项的快照
    monkeypatch.setattr(configs, "snapshot", lambda db, p: old)
    assert configs.info(None, None)["expert_pending"] == []
