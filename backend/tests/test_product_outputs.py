"""产品输出包组件清单自检（四期 C22）：每个交付件一个输出包，组件默认值在产品配置里定。

字段见 docs/接口约定.md 第二节“outputs”与 docs/交付链输入输出方案.md 第六节、8.1；
输出页、阶段包导出与改稿（A33、A35）按它取数。定制项可增减组件，增删须项目经理确认（平台规则，不在配置里）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PRODUCTS = ["soc", "ire", "dsm", "iso", "nsg"]
KINDS = {"data": {"xlsx", "platform"}, "doc": {"docx"}, "deck": {"pptx"}, "form": {"xlsx", "docx"}}
GEN = {"program", "model", "mixed"}
AUDIENCE = {"客户", "项目组", "管理层", "知识中心"}
PER = {"persona", "document"}
TRUTH = {"D3", "D4", "D5", "D7"}  # 结构化真源：D8 和改稿同步从这里取
FORBIDDEN = re.compile(r"测评结论|密评结论|认证结论|通过认证|合规结论|口令|密钥|漏洞细节|完整拓扑|IP ?规划|样本")


def _load(stem: str) -> dict:
    return yaml.safe_load((REPO / "products" / f"{stem}.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("stem", PRODUCTS)
def test_outputs(stem: str) -> None:
    cfg = _load(stem)
    outputs = cfg.get("outputs")
    assert outputs, f"{stem} 缺 outputs"
    codes = [d["code"] for d in cfg["deliverables"]]
    assert list(outputs) == [*codes, "closeout"], "每个交付件一个输出包，另加结项复盘 closeout"

    for key, comps in outputs.items():
        prefix = "R" if key == "closeout" else key
        assert comps, key
        assert [c["id"] for c in comps] == [f"{prefix}-C{i:02d}" for i in range(1, len(comps) + 1)], key
        names = [c["name"] for c in comps]
        assert len(set(names)) == len(names), key
        for c in comps:
            where = f"{stem} {c['id']}"
            assert c["name"].strip() and len(c["name"]) <= 30, where
            assert not FORBIDDEN.search(c["name"]), f"{where} 越过资质边界或红线 3"
            assert c["kind"] in KINDS and c["format"] in KINDS[c["kind"]], where
            assert c["gen"] in GEN, where
            assert c["to"] and set(c["to"]) <= AUDIENCE, where
            assert isinstance(c["default"], bool), where
            assert c.get("per", "persona") in PER, where
            assert set(c) <= {"id", "name", "kind", "format", "gen", "to", "default", "per", "truth", "client_signs"}, where
            if key in ("D1", "D2", "closeout"):
                assert c["gen"] == "program", f"{where}：D1、D2 与结项复盘走程序模板（负责人 11-28）"

        defaults = [c for c in comps if c["default"]]
        assert defaults, f"{key} 至少一个默认组件"
        truth = [c for c in comps if c.get("truth")]
        if key in TRUTH:
            assert len(truth) == 1 and truth[0]["kind"] == "data" and truth[0]["default"], f"{key} 须有且只有一个默认的数据表真源"
        else:
            assert not truth, f"{key} 不设真源"
        if key not in ("closeout",):
            assert any(c["kind"] in ("doc", "deck") for c in defaults), f"{key} 至少一个默认文稿或汇报（大纲成章处）"
        signs = [c for c in comps if c.get("client_signs")]
        assert len(signs) == (1 if key == "D1" else 0), f"{key}：只有 D1 范围确认单客户签字"

    assert any(c["kind"] == "deck" for c in outputs["D8"] if c["default"]), "D8 默认出高层汇报 PPT"
    assert any(c["to"] == ["知识中心"] for c in outputs["closeout"]), "结项复盘出经验沉淀候选包"


def test_config_still_parses() -> None:
    from app.products import config

    assert config.validate() == []
