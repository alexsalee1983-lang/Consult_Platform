"""SOC 搬进配置（二期 A2，“只搬不改”）：products/soc.yaml 渲染出来的交付链必须和一期写在代码里的完全一致。

下面的常量是一期 runs.py 里的原文（v1.0），只用于核对，改配置时这里不跟着改——要改提示词就是改行为，需另行验收。
"""
from __future__ import annotations

import pytest

from app import seed
from app.products import config
from app.project import runs

OLD_STEPS = [("facts", 3, "research-interview", "D3", "整理事实清单"),
             ("gap", 4, "compliance-basis", "", "差距分析"),
             ("draft", 7, "report-writer", "D4", "现状评估报告初稿")]
OLD_QUERIES = {"facts": "SOC 运营现状 事实 调研", "gap": "SOC 能力 差距分析 等保 基线", "draft": "SOC 现状评估报告 差距 建议"}
OLD_INSTRUCTIONS = {
    "facts": "任务：交付链第 3 步。根据下面的本项目证据，整理 D3 事实清单：按 SOC 能力域（组织与人员、流程、"
             "技术平台与数据源、度量与改进等）分章列出已确认的事实，每条带证据编号；另起一章列“待补证据与矛盾点”。"
             "只写证据里有的事实，不推测现状。",
    "gap": "任务：交付链第 4 步。以下面的 D3 事实清单为输入，对照评估基线（SOC 能力框架、等保 2.0、行业要求，"
           "依据只能来自知识库检索结果，没有的标【依据待核实】）做差距分析：输出差距分析表（能力项、现状、要求与依据、"
           "差距、证据充分性）、业务影响 TOP 5、依据库缺口。证据不足的项列出来并发“证据不足”确认卡。"
           "用“差距分析、符合性初判”的说法，不出具测评或认证结论。",
    "draft": "任务：交付链第 7 步。根据下面的差距分析和 D3 事实清单，起草 D4 现状评估报告初稿：评估范围与方法、"
             "现状概述、主要差距与业务影响、依据说明、后续建议方向。结论必须来自差距分析，并标注证据编号和依据；"
             "这是初稿，供顾问审核署名，不写最终结论口吻。D4 是交给客户的报告：不写内部工作内容（差距编号 G-xx、"
             "差距分析表原样照搬、确认卡、“测试”“内部”等字样），差距分析的结论改写成报告语言。",
}


def test_soc_chain_unchanged() -> None:
    chain = runs.chain_of(config.compose_codes("SOC"))
    assert [(s.key, s.number, s.agent, s.deliverable, s.name) for s in chain] == OLD_STEPS
    assert {s.key: s.instruction for s in chain} == OLD_INSTRUCTIONS
    assert {s.key: s.query for s in chain} == OLD_QUERIES
    assert {s.key: f"第 {s.number} 步 {s.name}" for s in chain} == {"facts": "第 3 步 整理事实清单", "gap": "第 4 步 差距分析", "draft": "第 7 步 现状评估报告初稿"}
    assert [runs._doc_input(s) for s in chain[1:]] == ["D3", "D3"]


def test_soc_deliverables_match_seed() -> None:
    prod = config.product("SOC")
    assert prod.name == "SOC 运营体系设计"
    assert [d.code for d in prod.deliverables] == [f"D{i + 1}" for i in range(8)]
    assert [d.title for d in prod.deliverables] == seed.DELIVERABLES


def test_broken_catalog_blocks_runs(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    (tmp_path / "products").mkdir()
    (tmp_path / "products" / "soc.yaml").write_text("code: [", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "catalog_dir", str(tmp_path))
    with pytest.raises(config.CatalogError):
        config.compose_codes("SOC")
