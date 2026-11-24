"""四期 B28②：引用译文条目要注明“非官方译本，以原文为准”并给原文条款号（md 草稿 + 评测题）。"""
from __future__ import annotations

import re
from pathlib import Path

from app.agents import evalset

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "knowledge" / "eval" / "agents" / "multilang.yaml"
COMMON = ROOT / "agents" / "01_公共规范.md"
LANGS = ("fr", "pt", "th", "id", "vi")


def test_common_rule_requires_notice_and_original_clause():
    text = COMMON.read_text(encoding="utf-8")
    line = next((x for x in text.splitlines() if "非官方译本" in x), "")
    assert "以原文为准" in line and "原文条款号" in line and "原文条目" in line


def test_multilang_eval_cases_check_notice_and_original_clause():
    cases = evalset.validate(evalset.load(EVAL))
    assert len(cases) >= 5
    for c in cases:
        must = " ".join(c.get("must", []))
        assert "非官方译本" in must and "以原文为准" in must, c["id"]
        assert re.search(r"KB-\d{6}", must), c["id"]          # 原文条目编号
        assert re.search(r"Art|มาตรา|Pasal|Điều", must), c["id"]   # 原文条款号
    langs = {lang for c in cases for lang in LANGS if f"language: {lang}" in c["prompt"]}
    assert langs == set(LANGS)


def test_must_not_skips_disclaimer_but_catches_affirmative():
    """compliance-basis.md 要求写“不构成测评结论”的免责语，must_not 只抓肯定式。"""
    cases = evalset.validate(evalset.load(EVAL))
    for c in cases:
        pats = c.get("must_not", [])
        assert pats, c["id"]
        ok = "本回答为依据梳理，不构成测评结论。"
        assert not any(re.search(p, ok) for p in pats), c["id"]
        assert not any(re.search(p, "不构成等保测评结论") for p in pats), c["id"]
        assert any(re.search(p, "测评结论为通过") for p in pats), c["id"]
        assert any(re.search(p, "可出具认证结论") for p in pats), c["id"]
