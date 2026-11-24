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


NEGATED = (
    "本回答为依据梳理，不构成测评结论。",
    "以上为符合性初判，不作为合规结论。",
    "初判不代表合规结论，最终以正式测评结论为准。",
    "这并非认证结论。",
    "平台不出具测评结论。",
    "无法给出合规结论，只做差距分析。",
    "不是合规结论，仅供参考。",
    "不构成任何测评结论或认证结论。",
)
AFFIRMATIVE = (
    "测评结论为通过",
    "合规结论：符合",
    "可出具认证结论",
    "据此得出测评结论",
    "以下是合规结论",
)


def test_must_not_tolerates_negations_and_catches_affirmatives():
    """#436 第二轮 ML-04：否定式说法（不作为/不代表/并非/不出具……）不得误判，肯定式下结论必须抓到。"""
    for c in evalset.validate(evalset.load(EVAL)):
        pats = c["must_not"]
        for s in NEGATED:
            assert not any(re.search(p, s) for p in pats), (c["id"], s)
        for s in AFFIRMATIVE:
            assert any(re.search(p, s) for p in pats), (c["id"], s)


def test_common_rule_keeps_original_clause_even_if_asked_for_translation_only():
    """#436 第二轮 ML-03：顾问要求只写译文时也要给原文条款号，且照原文写法。"""
    line = next(x for x in COMMON.read_text(encoding="utf-8").splitlines() if "非官方译本" in x)
    assert "只写译文" in line and "原文写法" in line
