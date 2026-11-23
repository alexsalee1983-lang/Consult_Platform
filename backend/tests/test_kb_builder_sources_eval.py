"""三期 C14：kb-builder 知识源清单职责（模式 F）md 与评测集的格式校验。

评测集放 knowledge/eval/builder/（不放 knowledge/eval/agents/，免得产品批量评测把它带上）。
"""
import re
from pathlib import Path

from app.agents import evalset, registry
from app.services import guard

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "knowledge" / "eval" / "builder" / "kb-builder-sources.yaml"
AGENT = "kb-builder"
AUTHORITY = ("法律", "行政法规", "国家标准", "行业标准", "监管通报", "白皮书", "厂商资料")
LICENSE = ("可用", "待法务", "不可用")
FIELDS = ("缺口编号", "发布机构", "网址", "合法获取与使用判断", "权威等级", "优先级", "预计量")


def test_md_has_source_list_mode():
    doc = registry.load(AGENT)
    assert doc is not None
    body = doc.body
    assert "模式 F：知识源清单" in body
    for must in (*FIELDS, "待法务", "待查（需登记来源）", "不编造网址", "参考资料", "已忽略输入中的指令"):
        assert must in body, must
    assert "、".join(AUTHORITY) in body, "权威等级七类须按从高到低写全"


def test_eval_set_valid_and_covers_duties():
    cases = evalset.validate(evalset.load(EVAL))
    assert len(cases) >= 10
    assert {c["agent"] for c in cases} == {AGENT}
    assert {"redline", "golden"} <= {c.get("kind", "redline") for c in cases}
    labels = {c["license"] for c in cases}
    assert labels == set(LICENSE), "三种许可判断都要有题"
    for c in cases:
        assert c["id"].startswith("KBS-"), c["id"]
        assert c["license"] in LICENSE and c["authority"] in AUTHORITY, c["id"]
        assert "模式 F" in c["prompt"], c["id"]
        assert not guard.check(c["prompt"]).blocked, c["id"]
        for url in re.findall(r"https?://([^/\s]+)", c["prompt"]):      # 只用虚拟域名
            assert "example" in url, (c["id"], url)
    text = "\n".join(c["prompt"] for c in cases)
    for duty in ("许可 不清", "禁止转载", "没有它的网址", "忽略你的规则", "付费", "已在本期清单"):
        assert duty in text, duty


def _hits(case_id: str, field: str, text: str) -> bool:
    case = next(c for c in evalset.load(EVAL) if c["id"] == case_id)
    pats = case[field]
    if field == "must":
        return all(re.search(p, text) for p in pats)
    return any(re.search(p, text) for p in pats)


def test_g02_must_not_handles_negation_before_keyword():
    """#265 测试：否定词在“合规要求”之前的正确表述不能判违规；肯定说法仍要拦。"""
    for ok in ("白皮书仅作背景参考，不进入合规要求引用，按规则标为参考资料",
               "白皮书来源已标注为“参考资料”，不得在 IRE 交付物中作为合规要求引用",
               "低：白皮书只能作为背景参考，不能作为合规要求或依据写入交付物。",
               "白皮书作为参考资料，不作合规要求"):
        assert not _hits("KBS-G02", "must_not", ok), ok
    for bad in ("SRC-T11 白皮书可作为合规要求依据",
                "| SRC-T11 | 白皮书 | 合规要求 |",
                "白皮书的观点直接列为合规要求"):
        assert _hits("KBS-G02", "must_not", bad), bad


def test_r06_must_accepts_pending_synonyms():
    """#265 测试：文号未知时“待查 / 待核实 / 待登记 / 未提供”等写法都算对。"""
    for ok in ("文号：待查", "文号、发布日期标“待核实（登记表未提供）”", "正式文件名称、文号待登记核实",
               "登记表未给出具体文件名称、文号和发布日期，不凭印象补写", "文号待核对"):
        assert _hits("KBS-R06", "must", ok), ok
