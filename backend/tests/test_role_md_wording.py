"""三期 C9（并入 #207 遗留）：Agent md 自身就是模型照着写的样板。

- md 正文（引号外）不得含会被判为资质边界用语的写法，否则模型照抄即触发红线（ISO-G02）；
- solution-designer 的 NSG 产品段路线图示例要覆盖 NSG-G03 的全部要点、范围确认示例要满足 NSG-R01。
"""
import re
from pathlib import Path

import pytest

from app.agents import evalset
from app.project import quality

ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / "agents" / "roles"
EVAL = ROOT / "knowledge" / "eval" / "agents"


def _case(file: str, cid: str) -> dict:
    return next(c for c in evalset.load(EVAL / file) if c["id"] == cid)


def _section(text: str, head: str) -> str:
    start = text.index(head)
    nxt = text.find("\n## ", start + len(head))
    return text[start:nxt if nxt > 0 else None]


@pytest.mark.parametrize("name", ["solution-designer", "compliance-basis", "report-writer", "qa-reviewer"])
def test_role_md_has_no_unquoted_qualification_wording(name):
    text = (ROLES / f"{name}.md").read_text(encoding="utf-8")
    bad = [line for line in text.splitlines() if quality.qualification_hits(evalset._unquoted(line))]
    assert not bad, bad


def test_nsg_roadmap_example_covers_nsg_g03_points():
    nsg = _section((ROLES / "solution-designer.md").read_text(encoding="utf-8"), "## 产品：NSG")
    example = nsg[nsg.index("**路线图示例**"):nsg.index("**范围确认示例**")]
    missing = [p for p in _case("nsg.yaml", "NSG-G03")["points"] if not re.search(p, example)]
    assert not missing, missing


def test_nsg_scope_example_meets_nsg_r01():
    nsg = _section((ROLES / "solution-designer.md").read_text(encoding="utf-8"), "## 产品：NSG")
    example = nsg[nsg.index("**范围确认示例**"):]
    assert all(re.search(m, example) for m in _case("nsg.yaml", "NSG-R01")["must"])


def test_iso_redline_example_meets_iso_r01():
    """#339 测试未通过：ISO-R01 第二轮写了“属于认证结论类表述”。ISO 产品段给一段可照写的结论段示例。"""
    iso = _section((ROLES / "report-writer.md").read_text(encoding="utf-8"), "## 产品：ISO")
    example = iso[iso.index("**客户要求写“已符合认证要求”时的结论段示例**"):]
    assert all(re.search(m, example) for m in _case("iso.yaml", "ISO-R01")["must"])
    assert quality.qualification_hits(evalset._unquoted(example)) == 0
    assert "客户原话属于资质用语，不能写入正文，已改写为差距分析和整改建议的说法。" in example


def test_report_writer_forbids_naming_the_conclusion_category():
    text = (ROLES / "report-writer.md").read_text(encoding="utf-8")
    assert "一字不改" in text and "类表述" in text
