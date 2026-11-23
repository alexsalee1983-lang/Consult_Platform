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
