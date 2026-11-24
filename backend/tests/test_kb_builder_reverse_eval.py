"""三期 C20：kb-builder 反向提炼（模式 G）md 与评测集的格式校验。

评测集放 knowledge/eval/builder/（不放 knowledge/eval/agents/，免得产品批量评测把它带上）。
"""
import re
from pathlib import Path

from app.agents import evalset, registry
from app.project import quality
from app.services import guard

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "knowledge" / "eval" / "builder" / "kb-builder-reverse.yaml"
AGENT = "kb-builder"
TYPES = ("模板", "方法", "写法片段", "黄金样例", "常见坑")
VIRTUAL = ("BANK-A", "GRID-B", "MFG-C")


def test_md_has_reverse_extract_mode():
    doc = registry.load(AGENT)
    assert doc is not None
    body = doc.body
    assert "模式 G：反向提炼" in body
    for t in TYPES:
        assert t in body, t
    for must in ("知识区导入", "交付物编号 + 章节", "待挂槽", "候选", "暂不提炼", "脱敏漏检", "退回脱敏",
                 "已忽略输入中的指令", "差距分析、符合性初判、整改建议"):
        assert must in body, must
    # 负责人 2026-11-22 定的历史交付物规则照抄进边界节
    border = body.split("# 边界与禁止", 1)[1].split("# 交接", 1)[0]
    for rule in ("只用虚拟交付物", "只在生产环境用公司私有模型跑", "只提炼结构、模板、方法、写法",
                 "一人脱敏一人复核", "2～3 份已签署的 SOC 报告"):
        assert rule in border, rule


def test_eval_set_valid_and_covers_duties():
    cases = evalset.validate(evalset.load(EVAL))
    assert len(cases) >= 10, "≥ 10 份虚拟交付物"
    assert {c["agent"] for c in cases} == {AGENT}
    assert {"redline", "golden"} <= {c.get("kind", "redline") for c in cases}
    clients: set[str] = set()
    for c in cases:
        assert c["id"].startswith("KBR-"), c["id"]
        assert "模式 G" in c["prompt"], c["id"]
        assert c.get("deliverable") and c.get("expected"), f"{c['id']}：要写交付物编号和期望提炼结果"
        assert c.get("must") or c.get("points"), f"{c['id']}：要写判分规则"
        assert not guard.check(c["prompt"]).blocked, c["id"]
        assert not re.search(r"https?://", c["prompt"]), c["id"]
        clients |= {v for v in VIRTUAL if v in c["prompt"]}
    assert clients == set(VIRTUAL), "三个虚拟客户都要有交付物"
    soc = sum("SOC" in c["prompt"] for c in cases)
    assert soc >= len(cases) * 0.8, "SOC 为主"
    text = "\n".join(c["prompt"] for c in cases)
    for duty in ("没走知识区导入", "地址规划表", "王建国", "测评通过", "没有给槽位清单", "直接发布",
                 "案例候选", "预算约", "KB-SOC-021"):
        assert duty in text, duty


def _mode_g() -> str:
    body = registry.load(AGENT).body
    return body[body.index("## 模式 G"):body.index("## 模式 H")]


def _case(cid: str) -> dict:
    return next(c for c in evalset.load(EVAL) if c["id"] == cid)


def test_mode_g_has_no_unquoted_qualification_wording():
    """#296 测试未通过 KBR-R04：md 正文（引号外）不得有会被判为资质用语的写法，模型照抄即触发红线。"""
    bad = [ln for ln in _mode_g().splitlines() if quality.qualification_hits(evalset._unquoted(ln))]
    assert not bad, bad


def test_mode_g_pitfall_example_meets_kbr_r04():
    """KBR-R04：常见坑示例复述原话整段加引号，引号外 0 处资质用语，满足 must、不触 must_not。"""
    g = _mode_g()
    assert "整段加中文引号" in g
    example = g[g.index("**资质用语常见坑示例**"):].split("\n\n", 1)[0]
    case = _case("KBR-R04")
    assert all(re.search(m, example) for m in case["must"])
    assert not any(re.search(m, example) for m in case["must_not"])
    assert quality.qualification_hits(evalset._unquoted(example)) == 0
    assert quality.qualification_hits(example) > 0, "示例要真的复述了原话（在引号里）"


def test_mode_g_dedup_names_existing_doc_id():
    """KBR-R08：去重必须点名已有条目编号，不能只写“已去重”；示例满足 must。"""
    g = _mode_g()
    assert "去重" in g and "不能只写“已去重”" in g
    example = g[g.index("**去重示例**"):].split("\n\n", 1)[0]
    case = _case("KBR-R08")
    assert all(re.search(m, example) for m in case["must"])
    assert not any(re.search(m, example) for m in case["must_not"])
