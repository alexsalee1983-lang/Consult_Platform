"""服务产品与模块配置的校验（二期 A1，docs/接口约定.md 第二、三节）。

test_repo_catalog_is_valid 是 CI 把关：仓库里 products/、modules/ 的配置有任何问题都失败（开发线 2 的配置草稿靠它检查）。
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from app.products import config

REPO = Path(__file__).resolve().parents[2]
AGENTS = {"research-interview", "compliance-basis", "report-writer"}

PRODUCT = {
    "code": "SOC",
    "name": "SOC 运营体系设计",
    "domain": "OPS",
    "status": "ready",
    "modules": ["DJBH"],
    "deliverables": [
        {"code": "D1", "title": "启动包", "stage": "启动", "client_facing": True, "outline": ["项目范围"]},
        {"code": "D2", "title": "事实清单", "stage": "调研", "client_facing": False},
        {"code": "D3", "title": "现状评估报告", "stage": "分析", "client_facing": True},
    ],
    "baseline": {"domains": ["组织与人员", "流程"], "frameworks": ["SOC 能力框架"], "basis": ["GB/T 22239-2019"]},
    "chain": [
        {"key": "facts", "number": 3, "name": "整理事实清单", "agent": "research-interview", "inputs": ["evidence"],
         "output": "D2", "query": "{product_name} 现状", "instruction": "按 {product_name} 能力域（{domains}）分章。"},
        {"key": "gap", "number": 4, "name": "差距分析", "agent": "compliance-basis", "inputs": ["D2"], "output": "",
         "query": "差距", "instruction": "对照 {frameworks}、{basis}。{module_instructions}"},
        {"key": "draft", "number": 7, "name": "初稿", "agent": "report-writer", "inputs": ["run:gap", "D2"],
         "output": "D3", "query": "报告", "instruction": "第 {number} 步。"},
    ],
    "gates": [
        {"id": "annex", "kind": "coverage", "deliverable": "D3", "items_file": "annex.txt", "level": "blocking",
         "message": "未覆盖全部控制项"},
        {"id": "no-cert", "kind": "forbidden_terms", "terms": ["通过认证"], "level": "blocking", "message": "用语"},
    ],
    "strict": {"card_items": ["监测能力"]},
}
MODULE = {
    "code": "DJBH", "name": "等保 2.0 整改与体系建设", "status": "ready", "products": ["SOC"],
    "adds": {"sections": {"SOC": {"D3": ["等保整改专项"]}}, "frameworks": ["等保 2.0 第三级"],
             "instructions": {"gap": "逐项对照第三级管理要求。"}, "kb_tags": ["等保"],
             "gates": [{"id": "djbh", "kind": "required_sections", "deliverable": "D3", "sections": ["等保整改专项"],
                        "level": "major", "message": "缺等保章节"}]},
}


def _write(base: Path, product: dict | None = None, module: dict | None = None) -> Path:
    (base / "products" / "rules").mkdir(parents=True, exist_ok=True)
    (base / "modules").mkdir(exist_ok=True)
    (base / "products" / "rules" / "annex.txt").write_text("# 附录 A\nA.5.1\nA.5.2\n", encoding="utf-8")
    if product is not None:
        (base / "products" / f"{product.get('code', 'x').lower()}.yaml").write_text(
            yaml.safe_dump(product, allow_unicode=True), encoding="utf-8")
    if module is not None:
        (base / "modules" / f"{module.get('code', 'x').lower()}.yaml").write_text(
            yaml.safe_dump(module, allow_unicode=True), encoding="utf-8")
    return base


def _problems(tmp_path: Path, product: dict | None = None, module: dict | None = None) -> list[str]:
    return config.load(_write(tmp_path, product, module), agents=AGENTS).problems


def test_repo_catalog_is_valid() -> None:
    cat = config.load(REPO)
    assert cat.problems == []


def test_valid_catalog_loads(tmp_path: Path) -> None:
    cat = config.load(_write(tmp_path, PRODUCT, MODULE), agents=AGENTS)
    assert cat.problems == []
    soc = cat.products["SOC"]
    assert [d.code for d in soc.deliverables] == ["D1", "D2", "D3"]
    assert [s.key for s in soc.chain] == ["facts", "gap", "draft"]
    assert soc.gates[0].params["items"] == ["A.5.1", "A.5.2"]
    assert cat.modules["DJBH"].sections == {"SOC": {"D3": ("等保整改专项",)}}
    assert len(soc.version) == 12


def test_version_ignores_formatting(tmp_path: Path) -> None:
    v1 = config.load(_write(tmp_path, PRODUCT, MODULE), agents=AGENTS).products["SOC"].version
    path = tmp_path / "products" / "soc.yaml"
    path.write_text("# 注释不改版本\n\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert config.load(tmp_path, agents=AGENTS).products["SOC"].version == v1
    changed = copy.deepcopy(PRODUCT)
    changed["name"] = "SOC 运营体系设计（改）"
    assert config.load(_write(tmp_path, changed, MODULE), agents=AGENTS).products["SOC"].version != v1


def _mutate(fn) -> dict:
    p = copy.deepcopy(PRODUCT)
    fn(p)
    return p


@pytest.mark.parametrize(("product", "expect"), [
    (_mutate(lambda p: p.update(code="soc1")), "2～6 位大写字母"),
    (_mutate(lambda p: p.update(code="GEN")), "GEN"),
    (_mutate(lambda p: p.update(domain="SEC")), "一级目录"),
    (_mutate(lambda p: p.update(status="live")), "ready 或 draft"),
    (_mutate(lambda p: p["deliverables"][1].update(code="D5")), "D2"),
    (_mutate(lambda p: p["deliverables"][0].update(stage="交付")), "stage"),
    (_mutate(lambda p: p["deliverables"][0].pop("client_facing")), "client_facing"),
    (_mutate(lambda p: p["chain"][0].update(instruction="{customer} 的现状")), "不允许的变量"),
    (_mutate(lambda p: p["chain"][0].update(instruction="写成 {json")), "花括号"),
    (_mutate(lambda p: p["chain"][0].update(agent="no-such-agent")), "no-such-agent"),
    (_mutate(lambda p: p["chain"][0].update(inputs=["run:gap"])), "run:gap"),
    (_mutate(lambda p: p["chain"][0].update(output="D9")), "D9"),
    (_mutate(lambda p: p["chain"][1].update(key="facts")), "重复"),
    (_mutate(lambda p: p["chain"][2].update(number=2)), "递增"),
    (_mutate(lambda p: p["gates"][0].update(kind="python")), "kind"),
    (_mutate(lambda p: p["gates"][0].update(items_file="../../secret.txt")), "rules/"),
    (_mutate(lambda p: p["gates"][0].update(items=["A.5.1"])), "其中一个"),
    (_mutate(lambda p: p["gates"][1].update(id="annex")), "重复"),
    (_mutate(lambda p: p["gates"][0].update(deliverable="D7")), "D7"),
    (_mutate(lambda p: p["chain"][0].update(instruction="测试口令：password=Abc@12345678")), "机密级"),
    (_mutate(lambda p: p.update(code="ISO")), "PRODUCTS"),
    (_mutate(lambda p: p.update(chain=[])), "交付链步骤"),
], ids=lambda v: v if isinstance(v, str) else "")
def test_product_problems(tmp_path: Path, product: dict, expect: str) -> None:
    module = copy.deepcopy(MODULE)
    module["products"] = [product["code"]] if isinstance(product.get("code"), str) else ["SOC"]
    module["adds"]["sections"] = {}
    problems = _problems(tmp_path, product, module)
    assert any(expect in p for p in problems), problems


def test_draft_product_may_be_unregistered(tmp_path: Path) -> None:
    p = copy.deepcopy(PRODUCT)
    p.update(code="ISO", status="draft", modules=[], chain=[])
    assert _problems(tmp_path, p) == []


def test_module_cross_checks(tmp_path: Path) -> None:
    m = copy.deepcopy(MODULE)
    m["adds"]["sections"] = {"SOC": {"D8": ["章节"]}}
    m["adds"]["instructions"] = {"nope": "说明"}
    m["adds"]["gates"][0]["deliverable"] = "D8"
    problems = _problems(tmp_path, PRODUCT, m)
    assert any("没有交付件 D8" in p for p in problems)
    assert any("步骤 nope" in p for p in problems)
    assert any("gates djbh" in p for p in problems)


def test_product_and_module_must_list_each_other(tmp_path: Path) -> None:
    m = copy.deepcopy(MODULE)
    m["products"] = ["ISO"]
    m["adds"]["sections"] = {}
    problems = _problems(tmp_path, PRODUCT, m)
    assert any("模块的 products 没有 SOC" in p for p in problems)

    p = copy.deepcopy(PRODUCT)
    p["modules"] = ["REDTEAM"]
    problems = _problems(tmp_path / "b", p, MODULE)
    assert any("模块 REDTEAM 没有配置文件" in x for x in problems)
    assert any("产品 SOC 的 modules 没有 DJBH" in x for x in problems)


def test_unreadable_yaml(tmp_path: Path) -> None:
    _write(tmp_path)
    (tmp_path / "products" / "bad.yaml").write_text("code: [unclosed", encoding="utf-8")
    assert any("读不出来" in p for p in config.load(tmp_path, agents=AGENTS).problems)


def test_cli_catalog_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    from app import cli
    from app.core.config import get_settings

    _write(tmp_path, PRODUCT, MODULE)
    monkeypatch.setattr(get_settings(), "catalog_dir", str(tmp_path))
    monkeypatch.setattr(config, "_known_agents", lambda: AGENTS)
    assert cli.main(["catalog-check"]) == 0
    assert "产品 SOC" in capsys.readouterr().out

    (tmp_path / "products" / "bad.yaml").write_text("- 1\n", encoding="utf-8")
    assert cli.main(["catalog-check"]) == 1
