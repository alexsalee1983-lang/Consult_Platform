"""现场工具（field-tool/）与平台摘要卡规则一致。

field-tool.html 内置的 DEFAULT_CHECKS 必须等于 cards.field_checks()（改规则后运行 scripts/update_field_tool_checks.py）；
field-tool/test/cases.json 是两边共用的用例，平台在这里核对，现场工具在 node 测试里核对。
后端镜像里没有 field-tool/ 目录，相关测试跳过。
"""
import json
import re
from pathlib import Path

import pytest

from app.project import cards

ROOT = Path(__file__).resolve().parents[2] / "field-tool"
needs_tool = pytest.mark.skipif(not (ROOT / "field-tool.html").exists(), reason="没有 field-tool 目录")


@needs_tool
def test_embedded_checks_match_platform() -> None:
    html = (ROOT / "field-tool.html").read_text(encoding="utf-8")
    m = re.search(r"/\*CHECKS-BEGIN\*/const DEFAULT_CHECKS = (.*?);/\*CHECKS-END\*/", html, re.S)
    assert m, "没找到 DEFAULT_CHECKS"
    assert json.loads(m.group(1)) == cards.field_checks(), "规则变了：运行 python ../scripts/update_field_tool_checks.py"


@needs_tool
def test_shared_cases_match_platform() -> None:
    cases = json.loads((ROOT / "test" / "cases.json").read_text(encoding="utf-8"))
    assert len(cases) >= 20
    for c in cases:
        assert [k for k in cards.card_problems(c["text"]) if k != "机密级"] == c["kinds"], c["text"]


def test_onsite_ref_accepts_three_part_codes() -> None:
    for ok in ("D-03", "I-04-07", "O-12", "XC-1"):
        assert cards.ONSITE_REF_RE.fullmatch(ok), ok
    for bad in ("第3页", "D03", "D-03-07-01"):
        assert not cards.ONSITE_REF_RE.fullmatch(bad), bad


def test_template_includes_checks() -> None:
    checks = cards.field_checks()
    assert checks["schema"] == 1
    assert all(len(p) == 4 for p in checks["patterns"])
    assert {"re", "strip", "allow", "generic_head", "tail"} <= set(checks["org"])
