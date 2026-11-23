"""三期 C19 最小版：kb-reviewer 起草预审 + 专家输入只拦三类（md 与评测集结构检查，不调模型）。"""
from pathlib import Path

from app.agents import evalset
from app.kb import agent_review
from app.services import guard

ROOT = Path(__file__).resolve().parents[2]
MD = ROOT / "agents" / "roles" / "kb-reviewer.md"
EVAL = ROOT / "knowledge" / "eval" / "agents" / "kb.yaml"
VIRTUAL = ("BANK-A", "GRID-B", "MFG-C")


def _md() -> str:
    return MD.read_text(encoding="utf-8")


def test_md_has_two_review_modes_and_three_blocking_classes():
    text = _md()
    assert "version: 0.3" in text
    for word in ("起草预审", "专家输入", "审核方式"):
        assert word in text, word
    # 专家输入只拦三类：红线、资质用语、引用不存在
    for word in ("红线", "资质用语", "引用不存在"):
        assert word in text, word
    # 起草预审四查：引用、格式、脱敏、资质用语
    for word in ("引用", "格式", "脱敏", "资质用语"):
        assert word in text, word


def test_md_keeps_platform_json_contract():
    """不新增检查项代号：平台 CHECKS 七项仍都在 md 审核清单里，结论词仍只有通过 / 存疑。"""
    text = _md()
    for code in agent_review.CHECKS:
        assert f"| {code} |" in text, code
    assert "`通过`" in text and "`存疑`" in text


def test_kb_reviewer_eval_set():
    cases = evalset.validate(evalset.load(EVAL))
    assert len(cases) >= 10
    assert all(c["agent"] == "kb-reviewer" for c in cases)
    kinds = {c.get("kind", "redline") for c in cases}
    assert {"redline", "mine"} <= kinds
    modes = {"起草预审": 0, "专家输入": 0}
    for c in cases:
        assert not guard.check(c["prompt"]).blocked, c["id"]   # 题目能发给模型：不含机密级内容
        for m in modes:
            if f"审核方式：{m}" in c["prompt"]:
                modes[m] += 1
    assert modes["起草预审"] >= 4 and modes["专家输入"] >= 4, modes
    # 专家输入放宽：至少有题要求只提建议、不判存疑
    assert any("审核方式：专家输入" in c["prompt"] and any("存疑" in x for x in c.get("must_not", []))
               for c in cases)


def test_md_has_no_unquoted_qualification_wording():
    """#294 测试未通过：审核意见复述问题用语未加引号，评测按资质边界用语判红线。md 正文自身先不能有。"""
    from app.project import quality
    bad = [line for line in _md().splitlines() if quality.qualification_hits(evalset._unquoted(line))]
    assert not bad, bad


def test_md_requires_quoting_problem_wording_with_example():
    """md 写死：复述条目里的问题用语整段加引号；示例意见满足 KB-R01 且引号外 0 处资质用语。"""
    from app.project import quality
    text = _md()
    assert "整段加中文引号" in text
    example = text[text.index("**资质用语问题的写法示例**"):]
    example = example[:example.index("\n\n")]
    assert quality.qualification_hits(evalset._unquoted(example)) == 0
    assert quality.qualification_hits(example) > 0          # 确实复述了问题用语（在引号里）
    case = next(c for c in evalset.load(EVAL) if c["id"] == "KB-R01")
    assert all(__import__("re").search(m, example) for m in case["must"])
