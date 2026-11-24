"""v3.0 验收脚本（scripts/v3_acceptance.py，#404）自身的守护测试。

验收脚本按条目列出“已有自动化证据”的 pytest 用例；用例改名或删掉后脚本会悄悄少测，
这里逐个核对用例存在，且覆盖的条目正好是 #404 指定的第 2、5、6、7、10、11 条。
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "v3_acceptance.py"


def _load():
    spec = importlib.util.spec_from_file_location("v3_acceptance", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_items_are_the_six_assigned() -> None:
    mod = _load()
    assert sorted(mod.ITEMS) == [2, 5, 6, 7, 10, 11]
    for no, item in mod.ITEMS.items():
        assert item["title"], no
        assert item["tests"], f"第 {no} 条没有列用例"


def test_every_listed_test_exists() -> None:
    mod = _load()
    missing = []
    for no, item in mod.ITEMS.items():
        for nodeid in item["tests"]:
            file, _, func = nodeid.partition("::")
            path = ROOT / "backend" / file
            if not path.is_file() or func not in _functions(path):
                missing.append(f"第 {no} 条：{nodeid}")
    assert not missing, "验收脚本引用的用例不存在：\n" + "\n".join(missing)


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ({"pytest": True, "live": True}, "已达标"),
        ({"pytest": True, "live": None}, "已达标（仅自动化用例）"),
        ({"pytest": False, "live": True}, "未达标"),
        ({"pytest": True, "live": False}, "未达标"),
    ],
)
def test_verdict(results: dict, expected: str) -> None:
    assert _load().verdict(results["pytest"], results["live"]) == expected


def test_report_has_no_project_internals() -> None:
    """报告只写条目、结论和计数，不写快照内容（快照含项目 id，只留本机）。"""
    mod = _load()
    rows = [{"no": 7, "title": "x", "pytest": True, "live": True, "note": "已签署引用 3 处，退化 0"}]
    text = mod.render(rows, commit="abc1234", env="A")
    assert "abc1234" in text and "第 7 条" in text and "已达标" in text
    assert "project_id" not in text


def test_ref_counts_parses_cli_line_and_zero_keys_absent() -> None:
    mod = _load()
    out = "快照：deliverable 4，found 5，signed_unresolved 1，unresolved 1；哈希 abc\n"
    assert mod.ref_counts(out) == {"deliverable": 4, "found": 5, "signed_unresolved": 1, "unresolved": 1}
    assert mod.ref_counts("快照：found 2；哈希 x").get("signed_unresolved", 0) == 0
